"""Shared helpers for Ubuntu package build scripts (lib/ubuntu.py).

Import from a sibling package directory with:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib.ubuntu import ...

Convention: helpers raise BuildError on failure, which callers catch once at
the top of main() and report as `error: <message>` with a non-zero exit.
External commands are logged before they run (run_cmd) so progress is
visible.  Download helpers are idempotent -- they skip files already present
on disk.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


class BuildError(Exception):
    """Raised for any fatal build/check error; caught once in each script's main()."""


def fail(message: str) -> None:
    raise BuildError(message)


def run_cmd(argv: list, **kwargs) -> None:
    """Run an external command, printing the full argv first; raise on non-zero exit."""
    print(f"\n==> {' '.join(str(a) for a in argv)}")
    result = subprocess.run(argv, **kwargs)
    if result.returncode != 0:
        fail(f"Command failed (exit {result.returncode}): {' '.join(str(a) for a in argv)}")


def capture(argv: list, **kwargs) -> str:
    """Run an external command and return its trimmed stdout; raise on non-zero exit."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    result = subprocess.run(argv, **kwargs)
    if result.returncode != 0:
        fail(f"Command failed: {' '.join(str(a) for a in argv)}\n{result.stderr}")
    return result.stdout.strip()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def download(url: str, dest: Path) -> None:
    """Download url to dest; skip if dest already exists (cache-friendly)."""
    if dest.exists():
        print(f"==> Using cached {dest}")
        return
    run_cmd([
        "curl", "--fail", "--location",
        "--retry", "4", "--retry-delay", "2",
        "--output", dest, url,
    ])


def _file_digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    actual = _file_digest(path, "sha256")
    if actual != expected:
        fail(f"SHA-256 mismatch for {path}\n  expected: {expected}\n  actual:   {actual}")
    print(f"==> SHA-256 OK: {path.name}")


def verify_md5(path: Path, expected: str) -> None:
    actual = _file_digest(path, "md5")
    if actual != expected:
        fail(f"MD5 mismatch for {path}\n  expected: {expected}\n  actual:   {actual}")
    print(f"==> MD5 OK: {path.name}")


def assert_ubuntu_24() -> None:
    """Abort when the running system is not Ubuntu 24.04 (Noble Numbat)."""
    os_release = Path("/etc/os-release").read_text()
    if 'VERSION_ID="24.04"' not in os_release:
        fail("This script targets Ubuntu 24.04 (Noble Numbat) only.")


def apt_install_debs(debs: list) -> None:
    """Install a list of .deb files via `sudo apt-get install`; skips -dbgsym packages."""
    installable = [d for d in debs if "-dbgsym_" not in Path(d).name]
    if not installable:
        fail("apt_install_debs: no installable packages in the provided list")
    run_cmd(["sudo", "apt-get", "install", "-y", *installable])


def dpkg_version(pkg: str):
    """Return the installed Debian package version string, or None when not installed."""
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", pkg],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def version_key(version: str) -> tuple:
    """Sort key for dotted numeric version strings, e.g. "3.60.10" -> (3, 60, 10).

    Plain string comparison orders "3.60.10" before "3.60.2"; this numeric
    tuple key sorts version components correctly regardless of digit count.
    """
    return tuple(int(x) for x in re.findall(r"\d+", version))
