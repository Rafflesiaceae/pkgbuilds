#!/usr/bin/env python3
"""Build lazy-user-session-bus as a .deb on Ubuntu 24.04.

Run from the lazy-user-session-bus/ directory where the compiled binary lives.
Usage: ./ubuntu24.py [--check | --force] [--install]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nupkg_ubuntu import BuildError, apt_install_debs, dpkg_version, fail, run_cmd

PKG_NAME = "lazy-user-session-bus"
PKG_VER = "1-1"

CONTROL = f"""Package: {PKG_NAME}
Version: {PKG_VER}
Section: utils
Priority: optional
Architecture: all
Maintainer: Rafflesiaceae <rafflesiaceae.plant@gmail.com>
Depends: dash
Description: Send events to users session from anywhere, made while lazy
 This package allows sending events to user sessions from various
 environments, designed for ease of use.
"""


def do_check() -> int:
    """Print a version status report; return the process exit code.

    There is no upstream URL to check -- this is a locally compiled binary.
    """
    installed = dpkg_version(PKG_NAME)
    print(f"Package:      {PKG_NAME}")
    print(f"Installed:    {installed or '(not installed)'}")
    print(f"Builds:       {PKG_VER}")
    print("Upstream:     N/A (local binary, no public upstream)")

    if installed != PKG_VER:
        print("Status:       NEEDS BUILD")
        return 1
    print("Status:       UP TO DATE")
    return 0


def build(install: bool) -> None:
    start_dir = Path.cwd()
    build_dir = start_dir / f"{PKG_NAME}-deb"
    final_deb = start_dir / f"{PKG_NAME}_{PKG_VER}_all.deb"

    # Clean up any leftover artifacts from a previous run.
    if build_dir.exists():
        shutil.rmtree(build_dir)
    final_deb.unlink(missing_ok=True)

    # Standard Debian staging tree layout.
    (build_dir / "DEBIAN").mkdir(parents=True)
    (build_dir / "usr" / "bin").mkdir(parents=True)

    # The compiled binary must be present in the package directory.
    bin_src = start_dir / PKG_NAME
    if not bin_src.exists():
        fail(f"Source binary '{PKG_NAME}' not found in {start_dir}")

    bin_dest = build_dir / "usr" / "bin" / PKG_NAME
    shutil.copy2(bin_src, bin_dest)
    bin_dest.chmod(0o755)

    (build_dir / "DEBIAN" / "control").write_text(CONTROL)

    run_cmd(["dpkg-deb", "--build", build_dir])

    # dpkg-deb names the output after the staging directory; rename to the
    # standard Debian convention: name_version_arch.deb.
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

    if args.check and args.force:
        print("error: --check and --force are mutually exclusive", file=sys.stderr)
        return 1

    if args.check:
        return do_check()

    # Without --force, skip the build when the installed package is already current.
    if not args.force and dpkg_version(PKG_NAME) == PKG_VER:
        print(f"{PKG_NAME} {PKG_VER} is already installed. Use --force to rebuild.")
        return 0

    try:
        build(args.install)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
