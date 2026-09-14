#!/usr/bin/env nu
# Repack an upstream Neovim release tarball as a native .deb on Ubuntu 24.04.
# The tarball URL is passed as a positional argument (usually a GitHub release).
# Usage: nu ubuntu24.nu <tarball-url> [--install] [--skip-apt]

use ../.nupkg.ubuntu.nu *

# Fallback runtime deps when dpkg-shlibdeps cannot resolve the shared libraries.
const FALLBACK_DEPS = "libc6, libgcc-s1, libstdc++6, libuv1t64, libvterm0, libluajit-5.1-2"

def main [
    url: string             # URL to the Neovim upstream tarball (.tar.gz or .tar.xz)
    --install (-i)          # Install the produced .deb after building
    --skip-apt              # Skip the apt-get install of build prerequisites
] {
    let pkgname   = (try { $env.PKGNAME   } catch { "neovim" })
    let revision  = (try { $env.REVISION  } catch { "1" })
    let arch      = (try { $env.ARCH      } catch { capture ["dpkg", "--print-architecture"] })
    let prefix    = (try { $env.PREFIX    } catch { "/usr" })
    let section   = (try { $env.SECTION   } catch { "editors" })
    let priority  = (try { $env.PRIORITY  } catch { "optional" })
    let homepage  = (try { $env.HOMEPAGE  } catch { "https://neovim.io" })
    let hostname  = (try { capture ["hostname", "-f"] } catch { capture ["hostname"] })
    let user      = (capture ["whoami"])
    let maintainer = (try { $env.MAINTAINER } catch { $"($user) <($user)@($hostname)>" })

    let start_dir = (pwd)
    let workdir   = (capture ["mktemp", "-d"])
    let src_tar   = ($workdir | path join "src.tar")
    let unpack    = ($workdir | path join "unpack")
    let pkgroot   = ($workdir | path join "pkgroot")

    mkdir $unpack
    mkdir $pkgroot

    # Install minimal build prerequisites unless the caller opted out.
    if not $skip_apt {
        run-cmd ["sudo", "apt-get", "update"]
        run-cmd [
            "sudo", "apt-get", "install", "-y", "--no-install-recommends",
            "ca-certificates", "curl", "tar", "xz-utils", "gzip",
            "dpkg-dev", "file", "coreutils", "findutils"
        ]
    }

    download $url $src_tar

    print "Extracting..."
    run-cmd ["tar", "-xf", $src_tar, "-C", $unpack]

    # Locate the subtree root that contains the expected bin/lib/share layout.
    # Neovim tarballs typically have one top-level directory with these inside.
    let candidates = (
        capture ["find", $unpack, "-mindepth", "1", "-maxdepth", "3", "-type", "d"]
        | lines
        | where {|d|
            ($"($d)/bin" | path type) == "dir"
            and ($"($d)/lib" | path type) == "dir"
            and ($"($d)/share" | path type) == "dir"
        }
    )
    if ($candidates | is-empty) {
        fail "Could not find a bin/lib/share subtree in the tarball."
    }
    let root = ($candidates | first)

    if ($"($root)/bin/nvim" | path type) != "file" {
        fail $"Expected executable at ($root)/bin/nvim"
    }

    # Determine version: prefer URL-embedded vX.Y.Z, then ask nvim itself.
    let version = (try { $env.VERSION } catch {
        let url_match = ($url | parse --regex '.*/v(?P<v>[0-9]+\.[0-9]+\.[0-9]+)')
        if not ($url_match | is-empty) {
            $url_match.v.0
        } else {
            # Run nvim with its own lib dir on LD_LIBRARY_PATH to avoid link errors.
            let nvim_out = (do {
                with-env { LD_LIBRARY_PATH: $"($root)/lib:($root)/lib/nvim" } {
                    ^$"($root)/bin/nvim" --version
                }
            } | complete | get stdout | lines | first)
            let ver_match = ($nvim_out | parse --regex 'v(?P<v>[0-9]+\.[0-9]+\.[0-9]+[^\s]*)')
            if not ($ver_match | is-empty) {
                # Sanitise any characters illegal in Debian version strings.
                $ver_match.v.0 | str replace --all "/" "." | str replace --regex '[^A-Za-z0-9.+:~\-]' ""
            } else {
                $"0.0~repack+(date now | format date '%Y%m%d%H%M')"
            }
        }
    })

    let out_deb = (try { $env.OUT_DEB } catch {
        $start_dir | path join $"($pkgname)_($version)-($revision)_($arch).deb"
    })

    print $"Staging into package root ($prefix)..."
    run-cmd ["install", "-d", ($pkgroot | path join $prefix)]
    run-cmd ["cp", "-a", ($root | path join "bin"),   ($pkgroot | path join $prefix)]
    run-cmd ["cp", "-a", ($root | path join "lib"),   ($pkgroot | path join $prefix)]
    run-cmd ["cp", "-a", ($root | path join "share"), ($pkgroot | path join $prefix)]

    # Install license/copyright file if any of the common names are present.
    run-cmd ["install", "-d", ($pkgroot | path join $prefix "share" "doc" $pkgname)]
    let license_candidates = ["LICENSE", "LICENSE.txt", "COPYING", "COPYRIGHT"]
    let license_file = ($license_candidates | where {|f| ($root | path join $f | path type) == "file"} | first?)
    if $license_file != null {
        run-cmd [
            "install", "-m", "0644",
            ($root | path join $license_file),
            ($pkgroot | path join $prefix "share" "doc" $pkgname "copyright")
        ]
    }

    # Best-effort: ask dpkg-shlibdeps to derive runtime Depends from the binary.
    print "Computing Depends (best-effort)..."
    let shlibs_out = ($workdir | path join "shlibdeps.txt")
    let shlibs_err = ($workdir | path join "shlibdeps.err")
    "" | save --force $shlibs_out

    # Collect extra -l search paths: top lib dir plus immediate subdirs under lib/nvim.
    mut lflags = [
        $"-l($pkgroot | path join $prefix 'lib')"
        $"-l($pkgroot | path join $prefix 'lib' 'nvim')"
    ]
    let extra_subdirs = (
        do {
            ^find ($pkgroot | path join $prefix "lib" "nvim") -mindepth 1 -maxdepth 2 -type d
        } | complete | get stdout | lines | where {|l| not ($l | is-empty)}
    )
    for d in $extra_subdirs { $lflags = ($lflags | append $"-l($d)") }

    let shlibdeps_result = (do {
        ^dpkg-shlibdeps -O
            $"-e($pkgroot | path join $prefix 'bin' 'nvim')"
            ...$lflags
    } | complete)
    $shlibdeps_result.stdout | save --force $shlibs_out
    $shlibdeps_result.stderr | save --force $shlibs_err

    let deps_line = (
        open --raw $shlibs_out
        | lines
        | where {|l| $l | str starts-with "shlibs:Depends="}
        | first?
    )
    let deps = if $deps_line != null {
        $deps_line | str replace "shlibs:Depends=" ""
    } else {
        print --stderr "Warning: dpkg-shlibdeps could not determine Depends; using minimal fallback."
        print --stderr "---- dpkg-shlibdeps stderr ----"
        open --raw $shlibs_err | lines | each {|l| print --stderr $"  ($l)"}
        print --stderr "--------------------------------"
        $FALLBACK_DEPS
    }

    mkdir ($pkgroot | path join "DEBIAN")
    # du returns KB; Installed-Size must be in KB per the Debian policy.
    let installed_kb = (capture ["du", "-sk", $pkgroot] | split row "\t" | first | into int)

    let control = $"Package: ($pkgname)
Version: ($version)-($revision)
Section: ($section)
Priority: ($priority)
Architecture: ($arch)
Maintainer: ($maintainer)
Homepage: ($homepage)
Installed-Size: ($installed_kb)
Depends: ($deps)
Description: Neovim (repacked from upstream tarball)
"
    $control | save --force ($pkgroot | path join "DEBIAN" "control")

    run-cmd ["chmod", "0755", ($pkgroot | path join "DEBIAN")]
    run-cmd ["chmod", "0644", ($pkgroot | path join "DEBIAN" "control")]

    print $"Building .deb: ($out_deb)"
    run-cmd ["dpkg-deb", "--build", "--root-owner-group", $pkgroot, $out_deb]

    # Work dir is a mktemp directory; clean it up now that the .deb is written.
    rm --recursive --force $workdir

    print $"Done: ($out_deb)"

    if $install {
        apt-install-debs [$out_deb]
    } else {
        print $"Install with: sudo apt install ./($out_deb | path basename)"
    }
}
