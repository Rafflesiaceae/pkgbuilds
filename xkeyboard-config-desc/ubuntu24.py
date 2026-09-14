#!/usr/bin/env python3
"""Build and optionally install xkeyboard-config-desc as a .deb on Ubuntu 24.04.

Run from the xkeyboard-config-desc/ directory where the `desc` symbol file lives.
Usage: ./ubuntu24.py [--check | --force] [--install]
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nupkg_ubuntu import BuildError, apt_install_debs, dpkg_version, fail, run_cmd

PKG_NAME = "xkeyboard-config-desc"

# postinst script: register the custom layout in evdev.xml after installation.
POSTINST = """#!/bin/bash
set -e
python3 -c "
import xml.etree.ElementTree as ET

EVDEV_XML = '/usr/share/X11/xkb/rules/evdev.xml'
tree = ET.parse(EVDEV_XML)
root = tree.getroot()
layout_list = root.find('.//layoutList')
if layout_list is None:
    exit(0)

# Skip registration if the layout is already present.
for layout in layout_list.findall('layout'):
    name = layout.find('configItem/name')
    if name is not None and name.text == 'desc':
        exit(0)

new_layout = ET.SubElement(layout_list, 'layout')
config = ET.SubElement(new_layout, 'configItem')
ET.SubElement(config, 'name').text = 'desc'
ET.SubElement(config, 'shortDescription').text = 'desc'
ET.SubElement(config, 'description').text = 'German (desc)'
country_list = ET.SubElement(config, 'countryList')
ET.SubElement(country_list, 'iso3166Id').text = 'DE'
lang_list = ET.SubElement(config, 'languageList')
ET.SubElement(lang_list, 'iso639Id').text = 'deu'
ET.SubElement(new_layout, 'variantList')

ET.indent(tree, space='  ')
tree.write(EVDEV_XML, encoding='unicode', xml_declaration=True)
"
"""

# postrm script: remove the custom layout from evdev.xml on uninstall/purge.
POSTRM = """#!/bin/bash
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    python3 -c "
import xml.etree.ElementTree as ET

EVDEV_XML = '/usr/share/X11/xkb/rules/evdev.xml'
tree = ET.parse(EVDEV_XML)
root = tree.getroot()
layout_list = root.find('.//layoutList')
if layout_list is not None:
    for layout in layout_list.findall('layout'):
        name = layout.find('configItem/name')
        if name is not None and name.text == 'desc':
            layout_list.remove(layout)
    ET.indent(tree, space='  ')
    tree.write(EVDEV_XML, encoding='unicode', xml_declaration=True)
"
fi
"""


def compute_version(start_dir: Path):
    """Compute the package version from the SHA-256 of the `desc` file.

    Returns None when desc is not found (wrong directory).
    """
    desc_path = start_dir / "desc"
    if not desc_path.exists():
        return None
    raw_hash = hashlib.sha256(desc_path.read_bytes()).hexdigest()[:8]
    return f"0~{raw_hash}"


def do_check(start_dir: Path) -> int:
    """Print a version status report; return the process exit code.

    Version is content-addressed via SHA-256 of the `desc` symbol file,
    so there is no separate upstream to check.
    """
    installed = dpkg_version(PKG_NAME)
    would_build = compute_version(start_dir)
    would_build_str = would_build or "(desc not found - run from xkeyboard-config-desc/ directory)"

    print(f"Package:      {PKG_NAME}")
    print(f"Installed:    {installed or '(not installed)'}")
    print(f"Builds:       {would_build_str}")
    print("Upstream:     N/A (version is SHA-256 of the local desc file)")

    if would_build is None or installed != would_build:
        print("Status:       NEEDS BUILD")
        return 1
    print("Status:       UP TO DATE")
    return 0


def build(install: bool) -> None:
    start_dir = Path.cwd()

    # Version is derived from the first 8 hex chars of the symbol file's SHA-256
    # so the package version automatically tracks content changes.
    desc_path = start_dir / "desc"
    if not desc_path.exists():
        fail(f"Source file 'desc' not found in {start_dir}")

    pkg_ver = compute_version(start_dir)
    build_dir = start_dir / f"{PKG_NAME}-deb"
    final_deb = start_dir / f"{PKG_NAME}_{pkg_ver}_all.deb"

    if build_dir.exists():
        shutil.rmtree(build_dir)
    final_deb.unlink(missing_ok=True)

    # XKB symbols live under /usr/share/X11/xkb/symbols/.
    symbols_dir = build_dir / "usr" / "share" / "X11" / "xkb" / "symbols"
    symbols_dir.mkdir(parents=True)
    (build_dir / "DEBIAN").mkdir(parents=True)

    desc_dest = symbols_dir / "desc"
    shutil.copy2(desc_path, desc_dest)
    desc_dest.chmod(0o644)

    control = f"""Package: {PKG_NAME}
Version: {pkg_ver}
Section: x11
Priority: optional
Architecture: all
Maintainer: Rafflesiaceae <rafflesiaceae.plant@gmail.com>
Description: X keyboard configuration files for custom desc layout
 This package installs the 'desc' symbol file to the XKB directory
 and registers it in the XKB rules so it appears in keyboard settings.
"""
    (build_dir / "DEBIAN" / "control").write_text(control)

    # Write and make executable the maintainer scripts.
    postinst = build_dir / "DEBIAN" / "postinst"
    postinst.write_text(POSTINST)
    postinst.chmod(0o755)

    postrm = build_dir / "DEBIAN" / "postrm"
    postrm.write_text(POSTRM)
    postrm.chmod(0o755)

    run_cmd(["dpkg-deb", "--build", build_dir])

    # Rename output to the standard Debian convention: name_version_arch.deb.
    Path(f"{build_dir}.deb").rename(final_deb)

    print("------------------------------------------------")
    print(f"Package created: {final_deb.name}")

    if install:
        apt_install_debs([final_deb])
    else:
        print(f"Install with: sudo apt install ./{final_deb.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--install", action="store_true", help="Install the produced .deb after building")
    parser.add_argument("-c", "--check", action="store_true", help="Report version status without building")
    parser.add_argument("-f", "--force", action="store_true", help="Skip up-to-date check and always rebuild")
    args = parser.parse_args()

    start_dir = Path.cwd()

    if args.check and args.force:
        print("error: --check and --force are mutually exclusive", file=sys.stderr)
        return 1

    if args.check:
        return do_check(start_dir)

    # Without --force, skip the build when the installed package is already current.
    if not args.force:
        pkg_ver = compute_version(start_dir)
        if pkg_ver is not None and dpkg_version(PKG_NAME) == pkg_ver:
            print(f"{PKG_NAME} {pkg_ver} is already installed. Use --force to rebuild.")
            return 0

    try:
        build(args.install)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
