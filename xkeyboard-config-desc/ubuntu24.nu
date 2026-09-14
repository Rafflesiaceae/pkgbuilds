#!/usr/bin/env nu
# Build and optionally install xkeyboard-config-desc as a .deb on Ubuntu 24.04.
# Run from the xkeyboard-config-desc/ directory where the `desc` symbol file lives.
# Usage: nu ubuntu24.nu [--install]

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

def main [
    --install (-i)  # Install the produced .deb after building
] {
    let start_dir = (pwd)

    # Version is derived from the first 8 hex chars of the symbol file's SHA-256
    # so the package version automatically tracks content changes.
    let desc_path = ($start_dir | path join "desc")
    if not ($desc_path | path exists) {
        fail $"Source file 'desc' not found in ($start_dir)"
    }
    let raw_hash = (capture ["sha256sum", $desc_path] | split row " " | first | str substring 0..<8)
    let pkg_ver  = $"0~($raw_hash)"

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
