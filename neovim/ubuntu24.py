#!/usr/bin/env python3
"""Repack the latest upstream Neovim GitHub release as a native .deb on Ubuntu 24.04.

Usage: ./ubuntu24.py [--check | --force] [--install] [--skip-apt]

The latest release is always resolved from GitHub; there is no way to pin a
specific version or tarball URL.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cli
from lib.ubuntu import BuildError, apt_install_debs, capture, download, dpkg_version, fail, run_cmd

PKG_NAME = "neovim"
# Fallback runtime deps when dpkg-shlibdeps cannot resolve the shared libraries.
FALLBACK_DEPS = "libc6, libgcc-s1, libstdc++6, libuv1t64, libvterm0, libluajit-5.1-2"

GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/neovim/neovim/releases/latest"

# Maps a dpkg architecture name to the suffix used in Neovim's GitHub release
# asset filenames (nvim-linux-<suffix>.tar.gz).
RELEASE_ASSET_ARCH = {"amd64": "x86_64", "arm64": "arm64"}


_release_cache = None


def github_latest_release():
    """Fetch the latest Neovim release metadata from the GitHub API.

    Cached for the lifetime of the process: a plain --install run already
    calls this once via check_up_to_date() and again via build(), and
    unauthenticated GitHub API requests are capped at 60/hour, so doubling
    up needlessly eats into that budget.

    Returns None on network error or unexpected JSON shape.
    """
    global _release_cache
    if _release_cache is not None:
        return _release_cache

    # Authenticate when a token is available to get the much higher
    # 5000/hour rate limit instead of the 60/hour anonymous one.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    args = ["curl", "-fsSL"]
    if token:
        args += ["-H", f"Authorization: Bearer {token}"]
    args.append(GITHUB_LATEST_RELEASE_API)

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        _release_cache = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return _release_cache


def github_latest():
    """Return the latest Neovim release tag (without the leading 'v'), or None."""
    release = github_latest_release()
    if release is None:
        return None
    try:
        return release["tag_name"].lstrip("v")
    except KeyError:
        return None


def latest_release_asset(arch: str) -> tuple[str, str]:
    """Resolve the (version, download URL) of the latest release's Linux
    tarball for arch, a dpkg architecture name (e.g. "amd64", "arm64").

    Raises BuildError when the architecture is unsupported or the release
    metadata/asset cannot be found.
    """
    asset_arch = RELEASE_ASSET_ARCH.get(arch)
    if asset_arch is None:
        fail(f"No known Neovim release asset for architecture {arch!r}")
    release = github_latest_release()
    if release is None:
        fail("Could not fetch the latest Neovim release from GitHub.")
    version = release.get("tag_name", "").lstrip("v")
    if not version:
        fail("Latest Neovim release has no tag_name.")
    asset_name = f"nvim-linux-{asset_arch}.tar.gz"
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            return version, asset["browser_download_url"]
    fail(f"Latest Neovim release (v{version}) has no asset named {asset_name}")


def do_check() -> int:
    """Print a version status report comparing the installed package against
    the latest GitHub release; return the process exit code.
    """
    installed = dpkg_version(PKG_NAME)
    print(f"Package:      {PKG_NAME}")
    print(f"Installed:    {installed or '(not installed)'}")

    print("              (fetching GitHub latest...)")
    upstream = github_latest()
    if upstream is None:
        print("Upstream:     (could not fetch)")
        print("Status:       UNKNOWN (could not determine the latest release)")
        return 1
    print(f"Upstream:     {upstream} (github.com/neovim/neovim/releases)")

    would_build = f"{upstream}-1"
    print(f"Builds:       {would_build}")

    if installed != would_build:
        print("Status:       NEEDS BUILD")
        return 1
    print("Status:       UP TO DATE")
    return 0


def build(install: bool, skip_apt: bool) -> None:
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

    print("Resolving the latest Neovim release from GitHub...")
    version, url = latest_release_asset(arch)
    print(f"Latest release: v{version} ({url})")

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
    parser.add_argument("--skip-apt", action="store_true", help="Skip the apt-get install of build prerequisites")


def check_up_to_date(args) -> str | None:
    upstream = github_latest()
    if upstream and dpkg_version(PKG_NAME) == f"{upstream}-1":
        return f"{PKG_NAME} {upstream}-1 is already installed. Use --force to rebuild."
    return None


def do_build(args) -> None:
    build(args.install, args.skip_apt)


def main() -> int:
    return cli.run(
        build=do_build,
        do_check=lambda args: do_check(),
        check_up_to_date=check_up_to_date,
        description=__doc__,
        add_arguments=add_arguments,
    )


if __name__ == "__main__":
    sys.exit(main())
