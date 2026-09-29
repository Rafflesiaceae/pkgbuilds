#!/usr/bin/env python3
"""Repack the latest upstream Ghidra GitHub release as a native .deb on Ubuntu 24.04.

Usage: ./ubuntu24.py [--check | --force] [--install] [--skip-apt]

The latest release is always resolved from GitHub; there is no way to pin a
specific version. Ghidra's release zip bundles native support binaries for
several platforms (Linux/macOS/Windows, x86_64/arm64), so the resulting
package is Architecture: all.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cli
from lib.ubuntu import BuildError, apt_install_debs, capture, download, dpkg_version, fail, run_cmd, verify_sha256

PKG_NAME = "ghidra"
# Best-effort runtime dependency: Ghidra requires a JDK (21+ as of the 11.x/12.x
# line); list a few known-good candidates so apt can satisfy whichever is
# available on the target release, falling back to the distro default.
JDK_DEPENDS = "openjdk-21-jdk | openjdk-22-jdk | openjdk-23-jdk | default-jdk"

GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/NationalSecurityAgency/ghidra/releases/latest"

# Matches release tags like "Ghidra_12.1.4_build".
TAG_RE = re.compile(r"^Ghidra_([\d.]+)_build$")
# Matches the single release asset, e.g. "ghidra_12.1.4_PUBLIC_20260921.zip".
ASSET_RE = re.compile(r"^ghidra_[\d.]+_PUBLIC_\d{8}\.zip$")


_release_cache = None


def github_token() -> str | None:
    """Resolve a token to authenticate GitHub API requests with.

    Checks GITHUB_TOKEN/GH_TOKEN first, then falls back to `gh auth token`
    (the GitHub CLI's stored credential) so an authenticated `gh` login
    alone is enough to get the 5000/hour rate limit -- no env var needed.
    Returns None (falling back to an anonymous request) when neither is
    available.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def github_latest_release():
    """Fetch the latest Ghidra release metadata from the GitHub API.

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
    token = github_token()
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


def github_latest() -> str | None:
    """Return the latest Ghidra version (e.g. "12.1.4"), or None."""
    release = github_latest_release()
    if release is None:
        return None
    match = TAG_RE.match(release.get("tag_name", ""))
    return match.group(1) if match else None


def latest_release_asset() -> tuple[str, str, str]:
    """Resolve the (version, download URL, sha256) of the latest release zip.

    Raises BuildError when the release metadata/asset cannot be found or
    doesn't match the expected shape.
    """
    release = github_latest_release()
    if release is None:
        fail("Could not fetch the latest Ghidra release from GitHub.")
    version = github_latest()
    if version is None:
        fail(f"Could not parse a version from tag_name {release.get('tag_name')!r}")
    for asset in release.get("assets", []):
        if ASSET_RE.match(asset.get("name", "")):
            digest = asset.get("digest", "")
            sha256 = digest.removeprefix("sha256:")
            if not sha256:
                fail(f"Release asset {asset['name']} has no sha256 digest.")
            return version, asset["browser_download_url"], sha256
    fail(f"Latest Ghidra release (tag {release.get('tag_name')!r}) has no matching zip asset.")


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
    print(f"Upstream:     {upstream} (github.com/NationalSecurityAgency/ghidra/releases)")

    would_build = f"{upstream}-1"
    print(f"Builds:       {would_build}")

    if installed != would_build:
        print("Status:       NEEDS BUILD")
        return 1
    print("Status:       UP TO DATE")
    return 0


def find_icon(root: Path) -> Path | None:
    """Best-effort search for an application icon inside the extracted tree."""
    for candidate in root.glob("**/GhidraIcon*.png"):
        return candidate
    return None


def build(install: bool, skip_apt: bool) -> None:
    pkgname = os.environ.get("PKGNAME", "ghidra")
    revision = os.environ.get("REVISION", "1")
    arch = "all"
    section = os.environ.get("SECTION", "devel")
    priority = os.environ.get("PRIORITY", "optional")
    homepage = os.environ.get("HOMEPAGE", "https://ghidra-sre.org")
    user = capture(["whoami"])
    try:
        hostname = capture(["hostname", "-f"])
    except BuildError:
        hostname = socket.gethostname()
    maintainer = os.environ.get("MAINTAINER", f"{user} <{user}@{hostname}>")

    print("Resolving the latest Ghidra release from GitHub...")
    version, url, sha256 = latest_release_asset()
    print(f"Latest release: {version} ({url})")

    start_dir = Path.cwd()
    workdir = Path(mkdtemp())
    src_zip = workdir / "src.zip"
    unpack = workdir / "unpack"
    pkgroot = workdir / "pkgroot"

    unpack.mkdir()
    pkgroot.mkdir()

    # Install minimal build prerequisites unless the caller opted out.
    if not skip_apt:
        run_cmd(["sudo", "apt-get", "update"])
        run_cmd([
            "sudo", "apt-get", "install", "-y", "--no-install-recommends",
            "ca-certificates", "curl", "unzip", "dpkg-dev", "coreutils", "findutils",
        ])

    download(url, src_zip)
    verify_sha256(src_zip, sha256)

    print("Extracting...")
    run_cmd(["unzip", "-q", src_zip, "-d", unpack])

    # The zip contains exactly one top-level directory (e.g.
    # ghidra_12.1.4_PUBLIC) with ghidraRun and friends directly inside it.
    entries = [p for p in unpack.iterdir() if p.is_dir()]
    if len(entries) != 1:
        fail(f"Expected exactly one top-level directory in the zip, found {len(entries)}.")
    root = entries[0]

    ghidra_run = root / "ghidraRun"
    if not ghidra_run.is_file():
        fail(f"Expected executable at {ghidra_run}")

    out_deb = Path(os.environ.get("OUT_DEB") or start_dir / f"{pkgname}_{version}-{revision}_{arch}.deb")

    # Ghidra is a self-contained Java application; stage the whole extracted
    # tree under /usr/share/ghidra rather than splitting into bin/lib/share.
    print("Staging into package root /usr/share/ghidra...")
    share_dir = pkgroot / "usr" / "share" / "ghidra"
    share_dir.parent.mkdir(parents=True)
    run_cmd(["cp", "-a", root, share_dir])

    # Wrapper so the app is on PATH; ghidraRun resolves its own install
    # directory via $0's realpath, so a plain symlink works.
    bin_dir = pkgroot / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    os.symlink("/usr/share/ghidra/ghidraRun", bin_dir / "ghidra")

    # Desktop entry for launcher menus; Icon is omitted if none is found
    # rather than pointing at a path that may not exist in this release.
    icon = find_icon(share_dir)
    applications_dir = pkgroot / "usr" / "share" / "applications"
    applications_dir.mkdir(parents=True)
    desktop_lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Ghidra",
        "Comment=Software reverse engineering (SRE) suite of tools",
        "Exec=ghidra",
        "Terminal=false",
        "Categories=Development;Security;",
    ]
    if icon is not None:
        desktop_lines.append(f"Icon={icon.stem}")
        icons_dir = pkgroot / "usr" / "share" / "icons" / "hicolor" / "128x128" / "apps"
        icons_dir.mkdir(parents=True)
        run_cmd(["install", "-m", "0644", icon, icons_dir / f"{icon.stem}.png"])
    (applications_dir / "ghidra.desktop").write_text("\n".join(desktop_lines) + "\n")

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
Depends: {JDK_DEPENDS}
Description: Ghidra software reverse engineering suite (repacked from upstream release)
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
