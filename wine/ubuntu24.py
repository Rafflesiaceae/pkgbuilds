#!/usr/bin/env python3
"""Build the latest upstream Wine release from source as a native .deb on Ubuntu 24.04.

Usage: ./ubuntu24.py [--check | --force] [--install] [--skip-apt] [--stable]

The latest release is always resolved from dl.winehq.org; there is no way to
pin a specific version. By default the newest release of any branch is built
(usually a development release such as 11.20); --stable restricts the lookup
to the stable X.0.y branch instead.

Wine is built in the "new WoW64" mode (--enable-archs=i386,x86_64): the 32-bit
side is PE-only and is cross-compiled with MinGW, so no i386 multiarch
development libraries are needed and the result is a single amd64 package
that runs both 32-bit and 64-bit Windows programs. The package contains the
complete `make install` tree under /usr, i.e. wine and wineserver plus all
the helper programs and development tools (winedbg, winecfg, wineconsole,
winepath, regedit, msiexec, winegcc, winebuild, widl, wrc, wmc, winedump,
winemaker, headers, ...).

It replaces Ubuntu's own wine/wine64/wine32/libwine packages as well as the
WineHQ winehq-* launcher packages, all of which ship conflicting files.

The release tarball is verified against its detached GPG signature, pinned to
the fingerprint of the Wine maintainer's release signing key.
"""

from __future__ import annotations

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
from lib.ubuntu import (
    BuildError, apt_install_debs, assert_ubuntu_24, capture, download,
    dpkg_version, fail, run_cmd, version_key,
)

PKG_NAME = "wine"

SOURCE_INDEX_URL = "https://dl.winehq.org/wine/source/"

# Alexandre Julliard's release signing key; every wine-X.Y.tar.xz on
# dl.winehq.org has a matching wine-X.Y.tar.xz.sign made with it.
SIGNING_KEY_FPR = "DA23579A74D4AD9AF9D3F945CEFAC8EAAF17519D"
# Tried in order until one of them hands out the key.
KEYSERVERS = ("hkps://keyserver.ubuntu.com", "hkps://keys.openpgp.org")

# Branch directories in the source index: "11.0/" holds the stable 11.0.y
# releases, "11.x/" the 11.y development releases.
BRANCH_DIR_RE = re.compile(r'href="(\d+\.(?:0|x))/"')
# Final release tarballs only; release candidates ("wine-11.0-rc5") are
# skipped because version_key() would sort them above the final release.
TARBALL_RE = re.compile(r'href="wine-(\d+(?:\.\d+)+)\.tar\.xz"')

# Packages providing files that overlap with a /usr-prefixed Wine install:
# Ubuntu's split wine packaging and the WineHQ launcher packages (which put
# /usr/bin/wine* symlinks into /opt/wine-<branch>).
CONFLICTS = [
    "libwine", "libwine-dev", "fonts-wine",
    "wine64", "wine64-preloader", "wine64-tools",
    "wine32", "wine32-preloader", "wine32-tools",
    "winehq-stable", "winehq-devel", "winehq-staging",
]

# Fallback runtime deps when dpkg-shlibdeps cannot resolve the shared libraries.
FALLBACK_DEPS = "libc6, libgcc-s1, libunwind8, libx11-6, libfreetype6, libfontconfig1"

# Build prerequisites. MinGW is needed for the PE side (both i386 and
# x86_64); its C++ compiler too, as without it configure silently drops the
# bundled C++ modules (fluidsynth for dmsynth MIDI, ICU, vcruntime140, ...).
# Everything else is the x86_64 development package of an optional feature,
# which configure picks up automatically when present.
BUILD_DEPS = [
    # Tooling, download and signature verification.
    "ca-certificates", "curl", "xz-utils", "gnupg", "dirmngr", "dpkg-dev", "file",
    "build-essential", "flex", "bison", "gettext", "pkgconf",
    "gcc-mingw-w64-i686", "gcc-mingw-w64-x86-64",
    "g++-mingw-w64-i686", "g++-mingw-w64-x86-64",
    # X11 driver.
    "libx11-dev", "libxext-dev", "libxcomposite-dev", "libxcursor-dev",
    "libxfixes-dev", "libxi-dev", "libxinerama-dev", "libxrandr-dev",
    "libxrender-dev", "libxxf86vm-dev",
    # Wayland driver.
    "libwayland-dev", "wayland-protocols", "libxkbcommon-dev", "libxkbregistry-dev",
    # Graphics.
    "libgl-dev", "libegl-dev", "libvulkan-dev", "ocl-icd-opencl-dev", "libva-dev",
    # Fonts and TLS.
    "libfreetype-dev", "libfontconfig-dev", "libgnutls28-dev",
    # Audio and multimedia.
    "libasound2-dev", "libpulse-dev",
    "libgstreamer1.0-dev", "libgstreamer-plugins-base1.0-dev",
    "libavcodec-dev", "libavformat-dev", "libavutil-dev",
    # Devices and system integration.
    "libdbus-1-dev", "libudev-dev", "libusb-1.0-0-dev", "libsdl2-dev",
    "libv4l-dev", "libgphoto2-dev", "libsane-dev", "libpcsclite-dev",
    "libcups2-dev", "libkrb5-dev", "libpcap-dev", "unixodbc-dev",
    # Unwinding of unix-side frames, used for winedbg backtraces.
    "libunwind-dev",
]


def fetch(url: str) -> str | None:
    """Return the body of url, or None on network/HTTP error."""
    result = subprocess.run(["curl", "-fsSL", url], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


_latest_cache: dict[bool, tuple[str, str] | None] = {}


def winehq_latest(stable: bool) -> tuple[str, str] | None:
    """Return (version, tarball URL) of the newest Wine release, or None.

    Only the two newest major versions' branch directories are scanned: a
    new major's development directory ("12.x/") shows up before its stable
    release does, so the previous major still has to be looked at to find
    e.g. the latest 11.0.y stable or the final 11.y development release.

    Cached per branch selection: a plain --install run resolves the version
    once via check_up_to_date() and again via build().
    """
    if stable in _latest_cache:
        return _latest_cache[stable]

    index = fetch(SOURCE_INDEX_URL)
    if index is None:
        return None
    branch_dirs = set(BRANCH_DIR_RE.findall(index))
    if stable:
        branch_dirs = {d for d in branch_dirs if d.endswith(".0")}
    majors = sorted({int(d.split(".")[0]) for d in branch_dirs}, reverse=True)[:2]

    candidates = []
    for branch_dir in sorted(branch_dirs):
        if int(branch_dir.split(".")[0]) not in majors:
            continue
        listing = fetch(f"{SOURCE_INDEX_URL}{branch_dir}/")
        if listing is None:
            return None
        for version in set(TARBALL_RE.findall(listing)):
            candidates.append((version, f"{SOURCE_INDEX_URL}{branch_dir}/wine-{version}.tar.xz"))

    _latest_cache[stable] = max(candidates, key=lambda c: version_key(c[0])) if candidates else None
    return _latest_cache[stable]


def verify_gpg_signature(path: Path, signature: Path, gnupg_home: Path) -> None:
    """Verify path against its detached signature using a throwaway keyring
    that only contains the pinned Wine release signing key.

    The key is fetched by full fingerprint (gpg rejects keyserver answers
    that don't match it), and the VALIDSIG status line is additionally
    checked so that a signature by any other key is never accepted.
    """
    gnupg_home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    for keyserver in KEYSERVERS:
        result = subprocess.run(
            ["gpg", "--batch", "--keyserver", keyserver, "--recv-keys", SIGNING_KEY_FPR],
            env=env, capture_output=True, text=True,
        )
        if result.returncode == 0:
            break
        print(f"==> Could not fetch the signing key from {keyserver}:\n{result.stderr}", file=sys.stderr)
    else:
        fail(f"Could not fetch Wine signing key {SIGNING_KEY_FPR} from any keyserver.")

    result = subprocess.run(
        ["gpg", "--batch", "--status-fd", "1", "--verify", signature, path],
        env=env, capture_output=True, text=True,
    )
    # VALIDSIG's last field is the fingerprint of the signer's primary key.
    valid = any(
        line.startswith("[GNUPG:] VALIDSIG ") and line.split()[-1] == SIGNING_KEY_FPR
        for line in result.stdout.splitlines()
    )
    if result.returncode != 0 or not valid:
        fail(f"GPG signature verification failed for {path}\n{result.stdout}{result.stderr}")
    print(f"==> GPG signature OK: {path.name} (key {SIGNING_KEY_FPR})")


def elf_files(root: Path) -> list[Path]:
    """Return all regular ELF files below root (unix-side binaries and libs).

    The PE side (*-windows directories) is skipped by virtue of not being
    ELF, so dpkg-shlibdeps is only handed files it can actually analyse.
    """
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as f:
            if f.read(4) == b"\x7fELF":
                found.append(path)
    return found


def shlib_depends(pkgroot: Path, prefix_dir: Path, workdir: Path) -> str:
    """Derive the runtime Depends from the libraries the ELF files link to.

    dpkg-shlibdeps insists on a debian/control in its working directory, so
    a minimal one is generated in a scratch directory. The package's own
    unix library directory is passed via -l so that e.g. ntdll.so (linked
    by the other unix libs) is recognised as shipped by this package.
    """
    shlibs_dir = workdir / "shlibdeps"
    (shlibs_dir / "debian").mkdir(parents=True)
    (shlibs_dir / "debian" / "control").write_text(
        f"Source: {PKG_NAME}\n\nPackage: {PKG_NAME}\nArchitecture: any\n"
    )

    lflags = [f"-l{d}" for d in sorted((prefix_dir / "lib" / "wine").glob("*-unix"))]
    result = subprocess.run(
        ["dpkg-shlibdeps", "-O", *lflags, *(f"-e{p}" for p in elf_files(pkgroot))],
        cwd=shlibs_dir, capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("shlibs:Depends="):
            return line[len("shlibs:Depends="):]

    print("Warning: dpkg-shlibdeps could not determine Depends; using minimal fallback.", file=sys.stderr)
    print("---- dpkg-shlibdeps stderr ----", file=sys.stderr)
    print(result.stderr, file=sys.stderr)
    print("--------------------------------", file=sys.stderr)
    return FALLBACK_DEPS


def dlopen_recommends(config_h: Path, exclude: set[str]) -> list[str]:
    """Map the libraries Wine dlopen()s at runtime to the packages providing them.

    Most optional features (fontconfig, GnuTLS, Vulkan, PulseAudio, CUPS,
    ...) are not linked but loaded on demand by SONAME, so dpkg-shlibdeps
    can't see them. configure records each one it found as a
    `#define SONAME_LIBFOO "libfoo.so.N"` in include/config.h; resolve those
    through the dynamic linker cache to the owning runtime package. These
    go into Recommends since Wine degrades gracefully when one is missing.
    """
    sonames = re.findall(r'^#define SONAME_\w+ "([^"]+)"', config_h.read_text(), re.MULTILINE)

    # `ldconfig -p` lines look like:
    #   libfoo.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libfoo.so.6
    ld_cache = {}
    for line in capture(["ldconfig", "-p"]).splitlines():
        match = re.match(r"\s*(\S+) \(([^)]*)\) => (\S+)", line)
        if match and "x86-64" in match.group(2):
            ld_cache.setdefault(match.group(1), match.group(3))

    packages = set()
    for soname in sonames:
        path = ld_cache.get(soname)
        if path is None:
            print(f"Warning: {soname} is not in the linker cache; not recommending it.", file=sys.stderr)
            continue
        # With merged /usr, dpkg may have the file registered under either
        # the /lib or the /usr/lib spelling, and either as the SONAME
        # symlink or as its fully versioned target.
        real = os.path.realpath(path)
        lookups = {path, real}
        lookups |= {"/usr" + p for p in lookups if not p.startswith("/usr/")}
        for lookup in sorted(lookups):
            result = subprocess.run(["dpkg-query", "-S", lookup], capture_output=True, text=True)
            if result.returncode == 0:
                # Output: "libfoo6:amd64: /usr/lib/x86_64-linux-gnu/libfoo.so.6"
                packages.add(result.stdout.split(":")[0].split(",")[0].strip())
                break
        else:
            print(f"Warning: no package owns {path}; not recommending it.", file=sys.stderr)

    return sorted(packages - exclude)


def do_check(stable: bool) -> int:
    """Print a version status report comparing the installed package against
    the latest WineHQ release; return the process exit code.
    """
    installed = dpkg_version(PKG_NAME)
    print(f"Package:      {PKG_NAME}")
    print(f"Installed:    {installed or '(not installed)'}")

    print("              (fetching WineHQ latest...)")
    latest = winehq_latest(stable)
    if latest is None:
        print("Upstream:     (could not fetch)")
        print("Status:       UNKNOWN (could not determine the latest release)")
        return 1
    upstream, _ = latest
    branch = "stable" if stable else "any branch"
    print(f"Upstream:     {upstream} (dl.winehq.org/wine/source, {branch})")

    would_build = f"{upstream}-1"
    print(f"Builds:       {would_build}")

    if installed != would_build:
        print("Status:       NEEDS BUILD")
        return 1
    print("Status:       UP TO DATE")
    return 0


def build(install: bool, skip_apt: bool, stable: bool) -> None:
    assert_ubuntu_24()

    pkgname = os.environ.get("PKGNAME", PKG_NAME)
    revision = os.environ.get("REVISION", "1")
    arch = capture(["dpkg", "--print-architecture"])
    prefix = os.environ.get("PREFIX", "/usr")
    section = os.environ.get("SECTION", "otherosfs")
    priority = os.environ.get("PRIORITY", "optional")
    homepage = os.environ.get("HOMEPAGE", "https://www.winehq.org")
    jobs = os.environ.get("JOBS") or capture(["nproc"])
    user = capture(["whoami"])
    try:
        hostname = capture(["hostname", "-f"])
    except BuildError:
        hostname = socket.gethostname()
    maintainer = os.environ.get("MAINTAINER", f"{user} <{user}@{hostname}>")

    # Only x86_64 hosts are supported: the WoW64 i386 side is cross-built
    # against an x86_64 unix side.
    if arch != "amd64":
        fail(f"Unsupported architecture {arch!r}; only amd64 is supported.")

    # Install build prerequisites unless the caller opted out. This comes
    # first because resolving the release needs curl.
    if not skip_apt:
        run_cmd(["sudo", "apt-get", "update"])
        run_cmd(["sudo", "apt-get", "install", "-y", "--no-install-recommends", *BUILD_DEPS])

    print("Resolving the latest Wine release from dl.winehq.org...")
    latest = winehq_latest(stable)
    if latest is None:
        fail("Could not determine the latest Wine release from dl.winehq.org.")
    version, url = latest
    print(f"Latest release: {version} ({url})")

    start_dir = Path.cwd()
    workdir = Path(mkdtemp())
    src_tar = workdir / f"wine-{version}.tar.xz"
    src_sig = workdir / f"wine-{version}.tar.xz.sign"
    src_dir = workdir / "src"
    build_dir = workdir / "build"
    pkgroot = workdir / "pkgroot"

    src_dir.mkdir()
    build_dir.mkdir()
    pkgroot.mkdir()

    download(url, src_tar)
    download(f"{url}.sign", src_sig)
    verify_gpg_signature(src_tar, src_sig, workdir / "gnupg")

    print("Extracting...")
    run_cmd(["tar", "-xf", src_tar, "-C", src_dir, "--strip-components=1"])

    # Build out of tree so the source stays pristine. Debug info is left out
    # by default to keep the package at a reasonable size (the unstripped
    # tree is several times larger); winedbg still gets export symbols.
    # Pass e.g. CFLAGS/CROSSCFLAGS="-g -O2" to build with debug info.
    env = os.environ.copy()
    env.setdefault("CFLAGS", "-O2")
    env.setdefault("CROSSCFLAGS", "-O2")
    run_cmd([
        src_dir / "configure",
        f"--prefix={prefix}",
        "--enable-archs=i386,x86_64",
        "--disable-tests",
    ], cwd=build_dir, env=env)

    run_cmd(["make", f"-j{jobs}"], cwd=build_dir, env=env)

    print(f"Staging into package root {prefix}...")
    run_cmd(["make", f"-j{jobs}", "install", f"DESTDIR={pkgroot}"], cwd=build_dir, env=env)

    prefix_dir = pkgroot / prefix.lstrip("/")
    bin_dir = prefix_dir / "bin"
    # Sanity-check the install: the loader, the server, the debugger and the
    # 32-bit PE side must all be present for this to be a usable package.
    for expected in (
        bin_dir / "wine", bin_dir / "wineserver", bin_dir / "winedbg",
        prefix_dir / "lib" / "wine" / "i386-windows" / "kernel32.dll",
        prefix_dir / "lib" / "wine" / "x86_64-windows" / "kernel32.dll",
    ):
        if not expected.exists():
            fail(f"Expected {expected.relative_to(pkgroot)} in the install tree, but it is missing.")

    # Recent Wine only ships a single `wine` loader for both 32- and 64-bit
    # programs. Keep a wine64 alias for scripts (e.g. older winetricks) that
    # still look for it.
    if not (bin_dir / "wine64").exists():
        os.symlink("wine", bin_dir / "wine64")

    # Desktop entry registering Wine as the handler for .exe/.msi files.
    applications_dir = prefix_dir / "share" / "applications"
    applications_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(["install", "-m", "0644", src_dir / "loader" / "wine.desktop", applications_dir / "wine.desktop"])

    doc_dir = prefix_dir / "share" / "doc" / pkgname
    doc_dir.mkdir(parents=True)
    for name in ("LICENSE", "COPYING.LIB", "AUTHORS", "README.md", "ANNOUNCE.md"):
        if (src_dir / name).is_file():
            target = "copyright" if name == "LICENSE" else name
            run_cmd(["install", "-m", "0644", src_dir / name, doc_dir / target])

    print("Computing Depends/Recommends...")
    deps = shlib_depends(pkgroot, prefix_dir, workdir)
    dep_names = {d.split()[0] for d in deps.split(", ")}
    recommends = dlopen_recommends(build_dir / "include" / "config.h", dep_names | {pkgname})

    out_deb = Path(os.environ.get("OUT_DEB") or start_dir / f"{pkgname}_{version}-{revision}_{arch}.deb")

    debian_dir = pkgroot / "DEBIAN"
    debian_dir.mkdir()
    # du returns KB; Installed-Size must be in KB per Debian policy.
    installed_kb = int(capture(["du", "-sk", pkgroot]).split("\t")[0])

    conflicts = ", ".join(CONFLICTS)
    control = f"""Package: {pkgname}
Version: {version}-{revision}
Section: {section}
Priority: {priority}
Architecture: {arch}
Maintainer: {maintainer}
Homepage: {homepage}
Installed-Size: {installed_kb}
Depends: {deps}
Recommends: {", ".join(recommends)}
Conflicts: {conflicts}
Replaces: {conflicts}
Provides: wine64
Description: Wine Windows compatibility layer (built from upstream source)
 Upstream Wine {version} built in WoW64 mode, running both 32-bit and
 64-bit Windows programs without i386 multiarch libraries. Includes
 wineserver, winedbg, winecfg and the other Wine programs as well as the
 development tools (winegcc, winebuild, widl, wrc, winedump, ...) and
 headers.
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
    parser.add_argument("--stable", action="store_true", help="Build the latest stable (X.0.y) release instead of the newest one")


def check_up_to_date(args) -> str | None:
    latest = winehq_latest(args.stable)
    if latest and dpkg_version(PKG_NAME) == f"{latest[0]}-1":
        return f"{PKG_NAME} {latest[0]}-1 is already installed. Use --force to rebuild."
    return None


def do_build(args) -> None:
    build(args.install, args.skip_apt, args.stable)


def main() -> int:
    return cli.run(
        build=do_build,
        do_check=lambda args: do_check(args.stable),
        check_up_to_date=check_up_to_date,
        description=__doc__,
        add_arguments=add_arguments,
    )


if __name__ == "__main__":
    sys.exit(main())
