#!/usr/bin/env nu
# Repack an upstream Neovim release tarball as a native .deb on Ubuntu 24.04.
# Usage: nu ubuntu24.nu [<tarball-url>] [--check | --force] [--install] [--skip-apt]
#
# --check without a URL still queries GitHub for the latest release and reports
# what is currently installed.

use ../.nupkg.ubuntu.nu *

const PKG_NAME = "neovim"
# Fallback runtime deps when dpkg-shlibdeps cannot resolve the shared libraries.
const FALLBACK_DEPS = "libc6, libgcc-s1, libstdc++6, libuv1t64, libvterm0, libluajit-5.1-2"

# Extract the Neovim version string from a GitHub release tarball URL.
# Returns null when no vX.Y.Z pattern is found.
def version-from-url [url: string] {
    let m = ($url | parse --regex '.*/v(?P<v>[0-9]+\.[0-9]+\.[0-9]+)')
    if ($m | is-empty) { null } else { $m.v.0 }
}

# Fetch the latest Neovim release tag from the GitHub API.
# Returns null on network error or unexpected JSON shape.
def github-latest [] {
    let result = (do {
        ^curl -fsSL "https://api.github.com/repos/neovim/neovim/releases/latest"
    } | complete)
    if $result.exit_code != 0 { return null }
    try {
        $result.stdout | from json | get tag_name | str replace "v" ""
    } catch {
        null
    }
}

# Print a version status report and exit.
# Checks the installed package, what a given URL would produce, and GitHub latest.
def do-check [url?: string] {
    let installed = (dpkg-version $PKG_NAME)
    let installed_str = if $installed == null { "(not installed)" } else { $installed }

    let url_version = if $url != null { version-from-url $url } else { null }
    let would_build = if $url_version != null { $"($url_version)-1" } else { null }
    let would_build_str = if $would_build != null { $would_build } else { "(no URL given)" }

    print $"Package:      ($PKG_NAME)"
    print $"Installed:    ($installed_str)"
    print $"Builds:       ($would_build_str)"

    # Always query GitHub so the user knows if there is a newer release.
    print "              (fetching GitHub latest...)"
    let upstream = (github-latest)
    let upstream_str = if $upstream != null {
        $"($upstream) (github.com/neovim/neovim/releases)"
    } else {
        "(could not fetch)"
    }
    print $"Upstream:     ($upstream_str)"

    # Determine whether any action is needed.
    let needs_build = $would_build != null and ($installed == null or $installed != $would_build)
    let newer_upstream = $upstream != null and $would_build != null and $upstream > $url_version
    let no_url_installed = $url == null and $installed == null

    if $needs_build {
        print "Status:       NEEDS BUILD"
        exit 1
    }
    if $newer_upstream {
        print "Status:       NEWER UPSTREAM AVAILABLE"
        exit 1
    }
    if $no_url_installed {
        print "Status:       NOT INSTALLED"
        exit 1
    }
    print "Status:       UP TO DATE"
}

def main [
    url?: string            # URL to the Neovim upstream tarball (.tar.gz or .tar.xz)
    --install  (-i)         # Install the produced .deb after building
    --check    (-c)         # Report version status without building; exits 0 (ok) or 1 (action needed)
    --force    (-f)         # Skip up-to-date check and always rebuild
    --skip-apt              # Skip the apt-get install of build prerequisites
] {
    if $check and $force {
        fail "--check and --force are mutually exclusive"
    }

    if $check {
        do-check $url
        return
    }

    # URL is required for building.
    if $url == null {
        fail "A tarball URL is required. Usage: nu ubuntu24.nu <url> [--install]"
    }

    # Without --force, skip the build when the installed package is already current.
    if not $force {
        let url_version = (version-from-url $url)
        if $url_version != null {
            let would_build = $"($url_version)-1"
            let installed = (dpkg-version $PKG_NAME)
            if $installed != null and $installed == $would_build {
                print $"($PKG_NAME) ($would_build) is already installed. Use --force to rebuild."
                return
            }
        }
    }

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
        let url_ver = (version-from-url $url)
        if $url_ver != null {
            $url_ver
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
