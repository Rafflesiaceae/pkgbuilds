#!/usr/bin/env nu
# Build and optionally install xkeyboard-config-desc as a .deb on Ubuntu 24.04.
# Run from the xkeyboard-config-desc/ directory where the `desc` symbol file lives.
# Usage: nu ubuntu24.nu [--check | --force] [--install]

use ../.nupkg.ubuntu.nu *

const PKG_NAME = "xkeyboard-config-desc"

# postinst script: register the custom layout in evdev.xml after installation.
const POSTINST = '#!/bin/bash
set -e
python3 -c "
import xml.etree.ElementTree as ET

EVDEV_XML = \'/usr/share/X11/xkb/rules/evdev.xml\'
tree = ET.parse(EVDEV_XML)
root = tree.getroot()
layout_list = root.find(\'.//layoutList\')
if layout_list is None:
    exit(0)

# Skip registration if the layout is already present.
for layout in layout_list.findall(\'layout\'):
    name = layout.find(\'configItem/name\')
    if name is not None and name.text == \'desc\':
        exit(0)

new_layout = ET.SubElement(layout_list, \'layout\')
config = ET.SubElement(new_layout, \'configItem\')
ET.SubElement(config, \'name\').text = \'desc\'
ET.SubElement(config, \'shortDescription\').text = \'desc\'
ET.SubElement(config, \'description\').text = \'German (desc)\'
country_list = ET.SubElement(config, \'countryList\')
ET.SubElement(country_list, \'iso3166Id\').text = \'DE\'
lang_list = ET.SubElement(config, \'languageList\')
ET.SubElement(lang_list, \'iso639Id\').text = \'deu\'
ET.SubElement(new_layout, \'variantList\')

ET.indent(tree, space=\'  \')
tree.write(EVDEV_XML, encoding=\'unicode\', xml_declaration=True)
"
'

# postrm script: remove the custom layout from evdev.xml on uninstall/purge.
const POSTRM = '#!/bin/bash
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    python3 -c "
import xml.etree.ElementTree as ET

EVDEV_XML = \'/usr/share/X11/xkb/rules/evdev.xml\'
tree = ET.parse(EVDEV_XML)
root = tree.getroot()
layout_list = root.find(\'.//layoutList\')
if layout_list is not None:
    for layout in layout_list.findall(\'layout\'):
        name = layout.find(\'configItem/name\')
        if name is not None and name.text == \'desc\':
            layout_list.remove(layout)
    ET.indent(tree, space=\'  \')
    tree.write(EVDEV_XML, encoding=\'unicode\', xml_declaration=True)
"
fi
'

# Compute the package version from the SHA-256 of the `desc` file.
# Returns null when desc is not found (wrong directory).
def compute-version [start_dir: string] {
    let desc_path = ($start_dir | path join "desc")
    if not ($desc_path | path exists) { return null }
    let raw_hash = (capture ["sha256sum", $desc_path] | split row " " | first | str substring 0..<8)
    $"0~($raw_hash)"
}

# Print a version status report and exit.
# Version is content-addressed via SHA-256 of the `desc` symbol file,
# so there is no separate upstream to check.
def do-check [start_dir: string] {
    let installed = (dpkg-version $PKG_NAME)
    let installed_str = if $installed == null { "(not installed)" } else { $installed }

    let would_build = (compute-version $start_dir)
    let would_build_str = if $would_build == null {
        "(desc not found — run from xkeyboard-config-desc/ directory)"
    } else {
        $would_build
    }

    print $"Package:      ($PKG_NAME)"
    print $"Installed:    ($installed_str)"
    print $"Builds:       ($would_build_str)"
    print $"Upstream:     N/A (version is SHA-256 of the local desc file)"

    if $would_build == null or $installed == null or $installed != $would_build {
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

    let start_dir = (pwd)

    if $check {
        do-check $start_dir
        return
    }

    # Version is derived from the first 8 hex chars of the symbol file's SHA-256
    # so the package version automatically tracks content changes.
    let desc_path = ($start_dir | path join "desc")
    if not ($desc_path | path exists) {
        fail $"Source file 'desc' not found in ($start_dir)"
    }

    let pkg_ver = (compute-version $start_dir)

    # Without --force, skip the build when the installed package is already current.
    if not $force {
        let installed = (dpkg-version $PKG_NAME)
        if $installed != null and $installed == $pkg_ver {
            print $"($PKG_NAME) ($pkg_ver) is already installed. Use --force to rebuild."
            return
        }
    }

    let build_dir = ($start_dir | path join $"($PKG_NAME)-deb")
    let final_deb = ($start_dir | path join $"($PKG_NAME)_($pkg_ver)_all.deb")

    # Clean up any leftover artifacts from a previous run.
    if ($build_dir | path exists) { rm --recursive --force $build_dir }
    if ($final_deb | path exists) { rm --force $final_deb }

    # XKB symbols live under /usr/share/X11/xkb/symbols/.
    mkdir ($build_dir | path join "DEBIAN")
    mkdir ($build_dir | path join "usr" "share" "X11" "xkb" "symbols")

    cp $desc_path ($build_dir | path join "usr" "share" "X11" "xkb" "symbols" "desc")
    run-cmd ["chmod", "644", ($build_dir | path join "usr" "share" "X11" "xkb" "symbols" "desc")]

    let control = $"Package: ($PKG_NAME)
Version: ($pkg_ver)
Section: x11
Priority: optional
Architecture: all
Maintainer: Rafflesiaceae <rafflesiaceae.plant@gmail.com>
Description: X keyboard configuration files for custom desc layout
 This package installs the 'desc' symbol file to the XKB directory
 and registers it in the XKB rules so it appears in keyboard settings.
"
    $control | save --force ($build_dir | path join "DEBIAN" "control")

    # Write and make executable the maintainer scripts.
    $POSTINST | save --force ($build_dir | path join "DEBIAN" "postinst")
    run-cmd ["chmod", "755", ($build_dir | path join "DEBIAN" "postinst")]

    $POSTRM | save --force ($build_dir | path join "DEBIAN" "postrm")
    run-cmd ["chmod", "755", ($build_dir | path join "DEBIAN" "postrm")]

    run-cmd ["dpkg-deb", "--build", $build_dir]

    # Rename output to the standard Debian convention: name_version_arch.deb.
    mv ($"($build_dir).deb") $final_deb

    print "------------------------------------------------"
    print $"Package created: ($final_deb | path basename)"

    if $install {
        apt-install-debs [$final_deb]
    } else {
        print $"Install with: sudo apt install ./($final_deb | path basename)"
    }
}
