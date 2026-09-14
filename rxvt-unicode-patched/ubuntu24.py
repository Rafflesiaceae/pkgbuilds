#!/usr/bin/env python3
"""Build rxvt-unicode 9.30 with custom patches as a .deb on Ubuntu 24.04.

Run from the rxvt-unicode-patched/ directory (where the patch files live).
Usage: ./ubuntu24.py [--check | --force] [--install]
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cli
from lib.ubuntu import apt_install_debs, capture, download, dpkg_version, ensure_dir, run_cmd

VERSION = "9.30"
PKG_NAME = "rxvt-unicode-patched"
SOURCE_URL = f"http://dist.schmorp.de/rxvt-unicode/Attic/rxvt-unicode-{VERSION}.tar.bz2"

# checkinstall formats the dpkg version as pkgversion-pkgrelease.
WOULD_BUILD = "9.30-1"

# Ordered list of patches to apply; missing files are skipped with a warning.
PATCHES = [
    "font-width-fix.patch",
    "line-spacing-fix.patch",
    "unicode-fix.patch",
    "disable-cursor-blink.patch",
    "fix-smart-resize-with-x11-frame-borders.patch",
    "rxvt-unicode-9.21-Fix-hard-coded-wrong-path-to-xsubpp.patch",
    "rxvt-unicode-0001-Prefer-XDG_RUNTIME_DIR-over-the-HOME.patch",
]


def do_check() -> int:
    """Print a version status report; return the process exit code.

    Upstream check is skipped: rxvt-unicode is effectively unmaintained
    (last official release 9.22 in 2016; we track the patched 9.30 from Attic).
    """
    installed = dpkg_version(PKG_NAME)
    print(f"Package:      {PKG_NAME}")
    print(f"Installed:    {installed or '(not installed)'}")
    print(f"Builds:       {WOULD_BUILD}")
    print("Upstream:     N/A (unmaintained; last official release 9.22)")

    if installed != WOULD_BUILD:
        print("Status:       NEEDS BUILD")
        return 1
    print("Status:       UP TO DATE")
    return 0


def build(install: bool) -> None:
    start_dir = Path.cwd()
    build_dir = start_dir / "build-urxvt"
    src_dir = build_dir / f"rxvt-unicode-{VERSION}"
    tarball = build_dir / f"rxvt-unicode-{VERSION}.tar.bz2"

    ensure_dir(build_dir)
    download(SOURCE_URL, tarball)

    # Always re-extract for a reproducible, patch-clean source tree.
    if src_dir.exists():
        shutil.rmtree(src_dir)
    run_cmd(["tar", "-xjf", tarball, "-C", build_dir])

    print(f"\nApplying patches from {start_dir}...")
    for p in PATCHES:
        patch_file = start_dir / p
        if patch_file.exists():
            print(f"  Applying {p}...")
            # -d changes into src_dir before applying, so strip count 0 is correct.
            run_cmd(["patch", "-p0", "-d", src_dir, "--input", patch_file])
        else:
            print(f"  Warning: {p} not found, skipping.")

    # Prepend PIE flags to any caller-supplied CFLAGS/CXXFLAGS.
    env = os.environ.copy()
    env["CFLAGS"] = f"-fPIE -pie {env.get('CFLAGS', '')}"
    env["CXXFLAGS"] = f"-fPIE -pie -std=c++11 {env.get('CXXFLAGS', '')}"

    run_cmd([
        "./configure",
        "--prefix=/usr",
        "--libdir=/usr/lib",
        "--enable-256-color",
        "--enable-combining",
        "--enable-fading",
        "--enable-font-styles",
        "--enable-iso14755",
        "--enable-keepscrolling",
        "--enable-mousewheel",
        "--enable-next-scroll",
        "--enable-perl",
        "--enable-pointer-blank",
        "--enable-rxvt-scroll",
        "--enable-selectionscrolling",
        "--enable-slipwheeling",
        "--disable-smart-resize",
        "--enable-startup-notification",
        "--enable-transparency",
        "--enable-unicode3",
        "--enable-xft",
        "--enable-xim",
        "--enable-xterm-scroll",
    ], cwd=src_dir, env=env)

    # doc/Makefile unconditionally invokes tic to install terminfo entries into
    # the system, which fails in user builds. Comment out those lines.
    doc_makefile = src_dir / "doc" / "Makefile"
    lines = doc_makefile.read_text().splitlines()
    lines = [f"#{line}" if re.match(r"^[ \t]*/usr/bin/tic", line) else line for line in lines]
    doc_makefile.write_text("\n".join(lines) + "\n")

    jobs = capture(["nproc"])
    run_cmd(["make", f"-j{jobs}"], cwd=src_dir, env=env)

    # The Makefile's install target tries to create /usr/lib/urxvt/perl but
    # does not create the parent urxvt/ directory first; pre-create it.
    run_cmd(["sudo", "mkdir", "-p", "/usr/lib/urxvt/perl"])

    maintainer = getpass.getuser()
    run_cmd([
        "sudo", "checkinstall", "-y",
        "--pkgname", PKG_NAME,
        "--pkgversion", VERSION,
        "--pkgrelease", "1",
        "--pkggroup", "x11",
        "--maintainer", maintainer,
        "--provides", "rxvt-unicode",
        "--requires", "libxft2,libperl5.38,libstartup-notification0,libnsl2,libptytty0",
        "--nodoc",
        "make", "install",
    ], cwd=src_dir)

    debs = sorted(src_dir.glob("*.deb"))
    if install:
        apt_install_debs(debs)
    else:
        print("--------------------------------------------------")
        print(f"Package built!  Install with: sudo dpkg -i {' '.join(str(d) for d in debs)}")


def check_up_to_date(args) -> str | None:
    if dpkg_version(PKG_NAME) == WOULD_BUILD:
        return f"{PKG_NAME} {WOULD_BUILD} is already installed. Use --force to rebuild."
    return None


def main() -> int:
    return cli.run(
        build=lambda args: build(args.install),
        do_check=lambda args: do_check(),
        check_up_to_date=check_up_to_date,
        description=__doc__,
    )


if __name__ == "__main__":
    sys.exit(main())
