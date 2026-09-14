#!/usr/bin/env python3
"""Repack an upstream Neovim release tarball as a native .deb on Ubuntu 24.04.

Usage: ./ubuntu24.py [<tarball-url>] [--check | --force] [--install] [--skip-apt]

--check without a URL still queries GitHub for the latest release and reports
what is currently installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cli
from lib.ubuntu import BuildError, apt_install_debs, capture, download, dpkg_version, fail, run_cmd, version_key

PKG_NAME = "neovim"
# Fallback runtime deps when dpkg-shlibdeps cannot resolve the shared libraries.
FALLBACK_DEPS = "libc6, libgcc-s1, libstdc++6, libuv1t64, libvterm0, libluajit-5.1-2"

VERSION_FROM_URL_RE = re.compile(r"/v(\d+\.\d+\.\d+)")
NVIM_VERSION_RE = re.compile(r"v(\d+\.\d+\.\d+\S*)")


def version_from_url(url: str):
    """Extract the Neovim version string from a GitHub release tarball URL."""
    m = VERSION_FROM_URL_RE.search(url)
    return m.group(1) if m else None


def github_latest():
    """Fetch the latest Neovim release tag from the GitHub API.

    Returns None on network error or unexpected JSON shape.
    """
    result = subprocess.run(
        ["curl", "-fsSL", "https://api.github.com/repos/neovim/neovim/releases/latest"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)["tag_name"].lstrip("v")
    except (json.JSONDecodeError, KeyError):
        return None


def do_check(url) -> int:
    """Print a version status report; return the process exit code.

    Checks the installed package, what a given URL would produce, and
    the latest release on GitHub.
    """
    installed = dpkg_version(PKG_NAME)
    url_version = version_from_url(url) if url else None
    would_build = f"{url_version}-1" if url_version else None

    print(f"Package:      {PKG_NAME}")
    print(f"Installed:    {installed or '(not installed)'}")
    print(f"Builds:       {would_build or '(no URL given)'}")

    print("              (fetching GitHub latest...)")
    upstream = github_latest()
    upstream_str = f"{upstream} (github.com/neovim/neovim/releases)" if upstream else "(could not fetch)"
    print(f"Upstream:     {upstream_str}")

    needs_build = would_build is not None and installed != would_build
    newer_upstream = (
        upstream is not None and url_version is not None
        and version_key(upstream) > version_key(url_version)
    )
    not_installed_no_url = url is None and installed is None

    if needs_build:
        print("Status:       NEEDS BUILD")
        return 1
    if newer_upstream:
        print("Status:       NEWER UPSTREAM AVAILABLE")
        return 1
    if not_installed_no_url:
        print("Status:       NOT INSTALLED")
        return 1
    print("Status:       UP TO DATE")
    return 0


def build(url: str, install: bool, skip_apt: bool) -> None:
    pkgname = os.environ.get("PKGNAME", "neovim")
    revision = os.environ.get("REVISION", "1")
    arch = os.environ.get("ARCH") or capture(["dpkg", "--print-architecture"])
    prefix = os.environ.get("PREFIX", "/usr")
    section = os.environ.get("SECTION", "editors")
    priority = os.environ.get("PRIORITY", "optional")
    homepage = os.environ.get("HOMEPAGE", "https://neovim.io")
    user = capture(["whoami"])
    try:
        hostname = capture(["hostname", "-f"])
    except BuildError:
        hostname = socket.gethostname()
    maintainer = os.environ.get("MAINTAINER", f"{user} <{user}@{hostname}>")

    start_dir = Path.cwd()
    workdir = Path(mkdtemp())
    src_tar = workdir / "src.tar"
    unpack = workdir / "unpack"
    pkgroot = workdir / "pkgroot"

    unpack.mkdir()
    pkgroot.mkdir()

    # Install minimal build prerequisites unless the caller opted out.
    if not skip_apt:
        run_cmd(["sudo", "apt-get", "update"])
        run_cmd([
            "sudo", "apt-get", "install", "-y", "--no-install-recommends",
            "ca-certificates", "curl", "tar", "xz-utils", "gzip",
            "dpkg-dev", "file", "coreutils", "findutils",
        ])

    download(url, src_tar)

    print("Extracting...")
    run_cmd(["tar", "-xf", src_tar, "-C", unpack])

    # Locate the subtree root that contains the expected bin/lib/share layout.
    # Neovim tarballs typically have one top-level directory with these inside.
    root = None
    for d in sorted(p for p in unpack.rglob("*") if p.is_dir()):
        if len(d.relative_to(unpack).parts) > 3:
            continue
        if (d / "bin").is_dir() and (d / "lib").is_dir() and (d / "share").is_dir():
            root = d
            break
    if root is None:
        fail("Could not find a bin/lib/share subtree in the tarball.")

    nvim_bin = root / "bin" / "nvim"
    if not nvim_bin.is_file():
        fail(f"Expected executable at {nvim_bin}")

    # Determine version: prefer URL-embedded vX.Y.Z, then ask nvim itself.
    version = os.environ.get("VERSION") or version_from_url(url)
    if not version:
        # Run nvim with its own lib dir on LD_LIBRARY_PATH to avoid link errors.
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = f"{root / 'lib'}:{root / 'lib' / 'nvim'}"
        result = subprocess.run([nvim_bin, "--version"], capture_output=True, text=True, env=env)
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        m = NVIM_VERSION_RE.search(first_line)
        if m:
            # Sanitise any characters illegal in Debian version strings.
            version = re.sub(r"[^A-Za-z0-9.+:~-]", "", m.group(1).replace("/", "."))
        else:
            version = f"0.0~repack+{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"

    out_deb = Path(os.environ.get("OUT_DEB") or start_dir / f"{pkgname}_{version}-{revision}_{arch}.deb")

    print(f"Staging into package root {prefix}...")
    prefix_dir = pkgroot / prefix.lstrip("/")
    prefix_dir.mkdir(parents=True)
    for sub in ("bin", "lib", "share"):
        run_cmd(["cp", "-a", root / sub, prefix_dir])

    # Install license/copyright file if any of the common names are present.
    doc_dir = prefix_dir / "share" / "doc" / pkgname
    doc_dir.mkdir(parents=True)
    for candidate in ("LICENSE", "LICENSE.txt", "COPYING", "COPYRIGHT"):
        license_file = root / candidate
        if license_file.is_file():
            run_cmd(["install", "-m", "0644", license_file, doc_dir / "copyright"])
            break

    # Best-effort: ask dpkg-shlibdeps to derive runtime Depends from the binary.
    print("Computing Depends (best-effort)...")
    lflags = [f"-l{prefix_dir / 'lib'}", f"-l{prefix_dir / 'lib' / 'nvim'}"]
    nvim_lib_dir = prefix_dir / "lib" / "nvim"
    if nvim_lib_dir.is_dir():
        for sub in sorted(p for p in nvim_lib_dir.iterdir() if p.is_dir()):
            lflags.append(f"-l{sub}")

    shlibdeps_result = subprocess.run(
        ["dpkg-shlibdeps", "-O", f"-e{prefix_dir / 'bin' / 'nvim'}", *lflags],
        capture_output=True, text=True,
    )
    deps = None
    for line in shlibdeps_result.stdout.splitlines():
        if line.startswith("shlibs:Depends="):
            deps = line[len("shlibs:Depends="):]
            break
    if deps is None:
        print("Warning: dpkg-shlibdeps could not determine Depends; using minimal fallback.", file=sys.stderr)
        print("---- dpkg-shlibdeps stderr ----", file=sys.stderr)
        print(shlibdeps_result.stderr, file=sys.stderr)
        print("--------------------------------", file=sys.stderr)
        deps = FALLBACK_DEPS

    debian_dir = pkgroot / "DEBIAN"
    debian_dir.mkdir()
    # du returns KB; Installed-Size must be in KB per Debian policy.
    installed_kb = int(capture(["du", "-sk", pkgroot]).split("\t")[0])

    control = f"""Package: {pkgname}
Version: {version}-{revision}
Section: {section}
Priority: {priority}
Architecture: {arch}
Maintainer: {maintainer}
Homepage: {homepage}
Installed-Size: {installed_kb}
Depends: {deps}
Description: Neovim (repacked from upstream tarball)
"""
    (debian_dir / "control").write_text(control)
    debian_dir.chmod(0o755)
    (debian_dir / "control").chmod(0o644)

    print(f"Building .deb: {out_deb}")
    run_cmd(["dpkg-deb", "--build", "--root-owner-group", pkgroot, out_deb])

    # Work dir is a mkdtemp directory; clean it up now that the .deb is written.
    shutil.rmtree(workdir)

    print(f"Done: {out_deb}")

    if install:
        apt_install_debs([out_deb])
    else:
        print(f"Install with: sudo apt install ./{out_deb.name}")


def add_arguments(parser) -> None:
    parser.add_argument("url", nargs="?", help="URL to the Neovim upstream tarball (.tar.gz or .tar.xz)")
    parser.add_argument("--skip-apt", action="store_true", help="Skip the apt-get install of build prerequisites")


def check_up_to_date(args) -> str | None:
    if not args.url:
        return None
    url_version = version_from_url(args.url)
    if url_version and dpkg_version(PKG_NAME) == f"{url_version}-1":
        return f"{PKG_NAME} {url_version}-1 is already installed. Use --force to rebuild."
    return None


def do_build(args) -> None:
    if not args.url:
        fail("a tarball URL is required. Usage: ./ubuntu24.py <url> [--install]")
    build(args.url, args.install, args.skip_apt)


def main() -> int:
    return cli.run(
        build=do_build,
        do_check=lambda args: do_check(args.url),
        check_up_to_date=check_up_to_date,
        description=__doc__,
        add_arguments=add_arguments,
    )


if __name__ == "__main__":
    sys.exit(main())
