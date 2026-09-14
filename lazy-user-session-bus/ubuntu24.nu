#!/usr/bin/env nu
# Build lazy-user-session-bus as a .deb on Ubuntu 24.04.
# Run from the lazy-user-session-bus/ directory where the compiled binary lives.
# Usage: nu ubuntu24.nu [--install]

use ../.nupkg.ubuntu.nu *

const PKG_NAME = "lazy-user-session-bus"
const PKG_VER  = "1-1"

def main [
    --install (-i)  # Install the produced .deb after building
] {
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
