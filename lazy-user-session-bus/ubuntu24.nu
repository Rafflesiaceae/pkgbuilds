#!/usr/bin/env nu
# Build lazy-user-session-bus as a .deb on Ubuntu 24.04.
# Run from the lazy-user-session-bus/ directory where the compiled binary lives.
# Usage: nu ubuntu24.nu [--check | --force] [--install]

use ../.nupkg.ubuntu.nu *

const PKG_NAME = "lazy-user-session-bus"
const PKG_VER  = "1-1"

# Print a version status report and exit.
# There is no upstream URL to check — this is a locally compiled binary.
def do-check [] {
    let installed = (dpkg-version $PKG_NAME)
    let installed_str = if $installed == null { "(not installed)" } else { $installed }

    print $"Package:      ($PKG_NAME)"
    print $"Installed:    ($installed_str)"
    print $"Builds:       ($PKG_VER)"
    print $"Upstream:     N/A (local binary, no public upstream)"

    if $installed == null or $installed != $PKG_VER {
        print "Status:       NEEDS BUILD"
        exit 1
    }
    print "Status:       UP TO DATE"
}

def main [
    --install (-i)  # Install the produced .deb after building
    --check   (-c)  # Report version status without building; exits 0 (ok) or 1 (action needed)
    --force   (-f)  # Skip up-to-date check and always rebuild
] {
    if $check and $force {
        fail "--check and --force are mutually exclusive"
    }

    if $check {
        do-check
        return
    }

    # Without --force, skip the build when the installed package is already current.
    if not $force {
        let installed = (dpkg-version $PKG_NAME)
        if $installed != null and $installed == $PKG_VER {
            print $"($PKG_NAME) ($PKG_VER) is already installed. Use --force to rebuild."
            return
        }
    }

    let start_dir = (pwd)
    let build_dir = ($start_dir | path join $"($PKG_NAME)-deb")
    let final_deb = ($start_dir | path join $"($PKG_NAME)_($PKG_VER)_all.deb")

    # Clean up any leftover artifacts from a previous run.
    if ($build_dir | path exists) { rm --recursive --force $build_dir }
    if ($final_deb | path exists) { rm --force $final_deb }

    # Standard Debian staging tree layout.
    mkdir ($build_dir | path join "DEBIAN")
    mkdir ($build_dir | path join "usr" "bin")

    # The compiled binary must be present in the package directory.
    let bin_src = ($start_dir | path join $PKG_NAME)
    if not ($bin_src | path exists) {
        fail $"Source binary '($PKG_NAME)' not found in ($start_dir)"
    }
    cp $bin_src ($build_dir | path join "usr" "bin" $PKG_NAME)
    run-cmd ["chmod", "755", ($build_dir | path join "usr" "bin" $PKG_NAME)]

    # Write the DEBIAN/control file that describes the package to dpkg.
    let control = $"Package: ($PKG_NAME)
Version: ($PKG_VER)
Section: utils
Priority: optional
Architecture: all
Maintainer: Rafflesiaceae <rafflesiaceae.plant@gmail.com>
Depends: dash
Description: Send events to users session from anywhere, made while lazy
 This package allows sending events to user sessions from various
 environments, designed for ease of use.
"
    $control | save --force ($build_dir | path join "DEBIAN" "control")

    run-cmd ["dpkg-deb", "--build", $build_dir]

    # dpkg-deb names the output after the staging directory; rename to the
    # standard Debian convention: name_version_arch.deb.
    mv ($"($build_dir).deb") $final_deb

    print "------------------------------------------------"
    print $"Package created: ($final_deb | path basename)"

    if $install {
        apt-install-debs [$final_deb]
    } else {
        print $"Install with: sudo apt install ./($final_deb | path basename)"
    }
}
