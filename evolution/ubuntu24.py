#!/usr/bin/env python3
"""Build and install Evolution 3.60.2 + Evolution Data Server 3.60.2
+ Evolution-EWS 3.60.2 as native .deb packages on Ubuntu 24.04 (Noble).

The packages use the normal Debian/Ubuntu package names and install prefix
(/usr, /etc), so they replace the distro Evolution packages rather than
creating a parallel /usr/local installation.

Build packaging base (Debian 3.56, intentionally chosen because its
file/package layout is much closer to GNOME 3.60 than Noble's older 3.52
packaging):
  evolution-data-server 3.56.2-8 (Debian)
  evolution             3.56.2-10 (Debian)
  evolution-ews         3.56.2-3 (Debian)

Upstream source for all three components is GNOME 3.60.2.

IMPORTANT:
  Evolution 3.60 bumps two EDS SONAMEs:
    libcamel-1.2.so.64          -> libcamel-1.2.so.67
    libebook-contacts-1.2.so.4  -> libebook-contacts-1.2.so.5

  Therefore this script creates NEW runtime packages
    libcamel-1.2-67t64
    libebook-contacts-1.2-5t64
  and intentionally lets Noble's old ABI packages coexist. Removing the old
  ABI packages could break Noble applications such as GNOME Contacts.

Output:
  ./evolution-3.60.2-deb-build/debs/

NOTE: --install is accepted for interface consistency but is a no-op here;
each build stage must install its output before the next stage can compile.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cli
from lib.ubuntu import (
    assert_ubuntu_24, capture, dpkg_version, ensure_dir,
    fail, run_cmd, download, verify_md5, verify_sha256, version_key,
)

UPSTREAM_VERSION = "3.60.2"
LOCAL_VERSION = "3.60.2-0ubuntu24.04.1+local1"

EDS_PACKAGING_VERSION = "3.56.2-8"
EVO_PACKAGING_VERSION = "3.56.2-10"
EWS_PACKAGING_VERSION = "3.56.2-3"

EDS_SHA256 = "2084dbdac396371b365d504c1ff45866ba8dca2f1252e5da1d3d9c33abdc1286"
EVO_SHA256 = "218a49fa5068155dbdbd35a30d10a2f237c72465fb1d0e0aa78889f039c2ed8f"
EWS_SHA256 = "940205818f6988411457ddf5804d210f3515c26e33415debd8fbb2731b1daf2b"

# Checksums published on packages.debian.org for the Debian packaging tarballs.
EDS_DEBIAN_MD5 = "ac50a68cb8f9a0f479a82598f59df606"
EVO_DEBIAN_MD5 = "323ebf28e4a611395b89c938d468b476"
EWS_DEBIAN_MD5 = "3b9d49ca08467b1ec2ae259932805554"


def replace_in_file(file: Path, old: str, new: str) -> None:
    if not file.exists():
        return
    text = file.read_text()
    if old in text:
        file.write_text(text.replace(old, new))


def clear_patch_series(src: Path) -> None:
    """Clear the patch series so debhelper does not try to apply Debian's
    3.56-targeted patches against the 3.60 source tree."""
    series = src / "debian" / "patches" / "series"
    if series.exists():
        series.write_text("")


def fix_dh_gnome_clean_for_noble(rules: Path) -> None:
    """Ubuntu 24.04's gnome-pkg-tools still makes dh_gnome_clean try to
    regenerate debian/control from debian/control.in. Modern Evolution
    packaging no longer ships control.in, so use the helper's supported
    --no-control mode while keeping the rest of the GNOME debhelper sequence.
    """
    before = rules.read_text()

    if "override_dh_gnome_clean:" in before:
        fixed = (
            before
            .replace("\tdh_gnome_clean\n", "\tdh_gnome_clean --no-control\n")
            .replace("dh_gnome_clean --no-control --no-control", "dh_gnome_clean --no-control")
        )
        rules.write_text(fixed)
    else:
        suffix = (
            "\n# Ubuntu 24.04 gnome-pkg-tools compatibility\n"
            "override_dh_gnome_clean:\n"
            "\tdh_gnome_clean --no-control\n"
        )
        rules.write_text(before + suffix)


def adapt_eds_build_deps_for_noble(control: Path) -> None:
    """Debian 3.56 uses the newer cross-build-friendly gir1.2-*-dev dependency
    names. Noble has the same development data, but some of those virtual
    package names are not satisfiable from Noble's archive metadata.

    Translate them to the concrete Noble development packages/providers.
    This changes only Build-Depends metadata; the compiled source and binary
    package contents remain upstream Evolution Data Server 3.60.2.
    """
    replace_in_file(control, "gir1.2-gio-2.0-dev", "gir1.2-glib-2.0-dev")
    replace_in_file(control, "gir1.2-gobject-2.0-dev", "gir1.2-glib-2.0-dev")
    replace_in_file(control, "gir1.2-gtk-3.0-dev", "libgtk-3-dev")
    replace_in_file(control, "gir1.2-gtk-4.0-dev", "libgtk-4-dev")
    replace_in_file(control, "gir1.2-icalglib-3.0-dev", "libical-dev")
    replace_in_file(control, "gir1.2-json-1.0-dev", "libjson-glib-dev")
    replace_in_file(control, "gir1.2-libxml2-2.0-dev", "libgirepository1.0-dev")
    replace_in_file(control, "gir1.2-soup-3.0-dev", "libsoup-3.0-dev")


def rename_package_helper_files(debian_dir: Path, old_name: str, new_name: str) -> None:
    for old_path in debian_dir.glob(f"{old_name}.*"):
        old_path.rename(debian_dir / old_path.name.replace(old_name, new_name))


def remove_if_present(base: Path, pattern: str) -> None:
    for file in base.glob(pattern):
        file.unlink()


def reset_component_dir(component_root: Path, source_dir: Path) -> None:
    if component_root.exists():
        shutil.rmtree(component_root)
    component_root.mkdir(parents=True)
    source_dir.mkdir()


def unpack_source(upstream_tar: Path, debian_tar: Path, source_dir: Path) -> None:
    run_cmd(["tar", "-xf", upstream_tar, "-C", source_dir, "--strip-components=1"])
    # Debian packaging tarballs contain debian/ at their root.
    run_cmd(["tar", "-xf", debian_tar, "-C", source_dir])


def add_local_changelog(source_dir: Path, component: str) -> None:
    run_cmd([
        "dch",
        "--newversion", LOCAL_VERSION,
        "--distribution", "noble",
        "--force-distribution",
        f"Local Ubuntu 24.04 build of upstream {component} {UPSTREAM_VERSION}.",
    ], cwd=source_dir)


def install_build_deps(source_dir: Path) -> None:
    argv = [
        "mk-build-deps", "--install", "--remove", "--root-cmd", "sudo",
        "--tool", "apt-get -y --no-install-recommends", "debian/control",
    ]
    print(f"\n==> {' '.join(argv)}")
    result = subprocess.run(argv, cwd=source_dir)

    if result.returncode != 0:
        print("\n==> mk-build-deps failed. Remaining unsatisfied Build-Depends:", file=sys.stderr)
        check = subprocess.run(
            ["dpkg-checkbuilddeps", "debian/control"],
            cwd=source_dir, capture_output=True, text=True,
        )
        if check.stdout.strip():
            print(check.stdout, file=sys.stderr)
        if check.stderr.strip():
            print(check.stderr, file=sys.stderr)
        fail(f"mk-build-deps failed with exit code {result.returncode}")


def build_component(source_dir: Path, jobs: str) -> None:
    run_cmd(["dpkg-buildpackage", "-b", "-us", "-uc", f"-j{jobs}"], cwd=source_dir)


def component_debs(component_root: Path) -> list:
    return sorted(component_root.glob("*.deb"))


def install_component_debs(component_root: Path) -> None:
    """Skip docs/tests during the staged host install; they are still copied
    to the final output directory. Dev/GIR packages are installed because
    the next component needs them at build time.
    """
    debs = [
        d for d in component_debs(component_root)
        if "-dbgsym_" not in d.name and "-doc_" not in d.name and "-tests_" not in d.name
    ]
    if not debs:
        fail(f"No .deb packages were produced in {component_root}")
    run_cmd(["sudo", "apt-get", "install", "-y", *debs])


def copy_component_debs(component_root: Path, out_dir: Path) -> None:
    for deb in component_debs(component_root):
        shutil.copy2(deb, out_dir)


def verify_no_usr_local(out_dir: Path) -> None:
    for deb in sorted(out_dir.glob("*.deb")):
        listing = capture(["dpkg-deb", "-c", deb])
        if "./usr/local/" in listing:
            fail(f"Package unexpectedly contains /usr/local files: {deb}")


def gnome_evolution_latest():
    """Fetch the latest patch release in the current GNOME Evolution series
    from the GNOME download server. Returns None on network error or parse
    failure.
    """
    # Derive the major.minor series directory from the hardcoded upstream version.
    series = ".".join(UPSTREAM_VERSION.split(".")[:2])
    result = subprocess.run(
        ["curl", "-fsSL", f"https://download.gnome.org/sources/evolution/{series}/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None

    # Directory listings contain hrefs like: evolution-3.60.X.tar.xz
    pattern = re.compile(rf"evolution-({re.escape(series)}\.\d+)\.tar\.xz")
    versions = sorted(set(pattern.findall(result.stdout)), key=version_key, reverse=True)
    return versions[0] if versions else None


def do_check() -> int:
    """Print a version status report for all three Evolution components;
    return the process exit code.

    Checks the installed `evolution` package against LOCAL_VERSION and
    queries the GNOME download server for newer patch releases in the
    wrapped series.
    """
    installed = dpkg_version("evolution")
    print("Package:      evolution (+ evolution-data-server, evolution-ews)")
    print(f"Installed:    {installed or '(not installed)'}")
    print(f"Builds:       {LOCAL_VERSION}")

    print("              (fetching GNOME latest...)")
    upstream = gnome_evolution_latest()
    upstream_str = f"{upstream} (download.gnome.org/sources/evolution)" if upstream else "(could not fetch)"
    print(f"Upstream:     {upstream_str}")

    needs_build = installed != LOCAL_VERSION
    # A newer upstream exists when the server reports a version beyond what
    # the script currently wraps, signalling that the script itself needs
    # updating.
    newer_upstream = upstream is not None and upstream != UPSTREAM_VERSION

    if needs_build:
        print("Status:       NEEDS BUILD")
        return 1
    if newer_upstream:
        print(f"Status:       NEWER UPSTREAM AVAILABLE - {upstream} vs script wraps {UPSTREAM_VERSION}")
        return 1
    print("Status:       UP TO DATE")
    return 0


def build() -> None:
    assert_ubuntu_24()

    os.environ["DEBFULLNAME"] = "Local Evolution Builder"
    os.environ["DEBEMAIL"] = "local-evolution-builder@localhost"
    os.environ["DEB_BUILD_OPTIONS"] = "nocheck"

    arch = capture(["dpkg", "--print-architecture"])
    jobs = capture(["nproc"])
    start_dir = Path.cwd()

    work = start_dir / "evolution-3.60.2-deb-build"
    downloads = work / "downloads"
    build_root = work / "build"
    out_dir = work / "debs"

    eds_root = build_root / "evolution-data-server"
    evo_root = build_root / "evolution"
    ews_root = build_root / "evolution-ews"

    eds_src = eds_root / "source"
    evo_src = evo_root / "source"
    ews_src = ews_root / "source"

    ensure_dir(work)
    ensure_dir(downloads)

    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir()

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir()

    print(f"Building Evolution {UPSTREAM_VERSION} for Ubuntu 24.04 / {arch}")
    print(f"Local package version: {LOCAL_VERSION}")
    print(f"Parallel build jobs: {jobs}")
    print(f"\nOutput will be written to:\n  {out_dir}")

    # Avoid changing files underneath a running Evolution process.
    running = subprocess.run(["pgrep", "-x", "evolution"], capture_output=True)
    if running.returncode == 0:
        fail("Evolution is currently running. Close it completely, then rerun this script.")

    print("\n==> Installing packaging/bootstrap tools")
    run_cmd(["sudo", "apt-get", "update"])
    run_cmd([
        "sudo", "apt-get", "install", "-y", "--no-install-recommends",
        "build-essential", "ca-certificates", "curl", "debhelper", "devscripts",
        "dpkg-dev", "equivs", "libdistro-info-perl", "libgirepository1.0-dev",
        "fakeroot", "gnome-pkg-tools", "pkg-config", "quilt", "xz-utils",
    ])

    eds_tar = downloads / f"evolution-data-server-{UPSTREAM_VERSION}.tar.xz"
    evo_tar = downloads / f"evolution-{UPSTREAM_VERSION}.tar.xz"
    ews_tar = downloads / f"evolution-ews-{UPSTREAM_VERSION}.tar.xz"

    eds_debian_tar = downloads / f"evolution-data-server_{EDS_PACKAGING_VERSION}.debian.tar.xz"
    evo_debian_tar = downloads / f"evolution_{EVO_PACKAGING_VERSION}.debian.tar.xz"
    ews_debian_tar = downloads / f"evolution-ews_{EWS_PACKAGING_VERSION}.debian.tar.xz"

    print("\n==> Downloading official GNOME 3.60.2 sources")
    download(f"https://download.gnome.org/sources/evolution-data-server/3.60/evolution-data-server-{UPSTREAM_VERSION}.tar.xz", eds_tar)
    download(f"https://download.gnome.org/sources/evolution/3.60/evolution-{UPSTREAM_VERSION}.tar.xz", evo_tar)
    download(f"https://download.gnome.org/sources/evolution-ews/3.60/evolution-ews-{UPSTREAM_VERSION}.tar.xz", ews_tar)

    verify_sha256(eds_tar, EDS_SHA256)
    verify_sha256(evo_tar, EVO_SHA256)
    verify_sha256(ews_tar, EWS_SHA256)

    print("\n==> Downloading recent Debian packaging metadata")
    download(f"https://deb.debian.org/debian/pool/main/e/evolution-data-server/evolution-data-server_{EDS_PACKAGING_VERSION}.debian.tar.xz", eds_debian_tar)
    download(f"https://deb.debian.org/debian/pool/main/e/evolution/evolution_{EVO_PACKAGING_VERSION}.debian.tar.xz", evo_debian_tar)
    download(f"https://deb.debian.org/debian/pool/main/e/evolution-ews/evolution-ews_{EWS_PACKAGING_VERSION}.debian.tar.xz", ews_debian_tar)

    verify_md5(eds_debian_tar, EDS_DEBIAN_MD5)
    verify_md5(evo_debian_tar, EVO_DEBIAN_MD5)
    verify_md5(ews_debian_tar, EWS_DEBIAN_MD5)

    # ------------------------------------------------------------------
    # 1. Evolution Data Server
    # ------------------------------------------------------------------
    print("\n============================================================")
    print("  1/3  evolution-data-server 3.60.2")
    print("============================================================")

    reset_component_dir(eds_root, eds_src)
    unpack_source(eds_tar, eds_debian_tar, eds_src)
    clear_patch_series(eds_src)

    eds_control = eds_src / "debian" / "control"
    eds_rules = eds_src / "debian" / "rules"
    eds_debian = eds_src / "debian"

    # Debian's current metadata is for 3.56.2. Tight build dependencies in the
    # package metadata need to point at our matched 3.60.2 stack.
    replace_in_file(eds_control, "3.56.2", UPSTREAM_VERSION)
    replace_in_file(eds_control, "3.57", "3.61")
    replace_in_file(eds_rules, "3.56.2", UPSTREAM_VERSION)
    replace_in_file(eds_rules, "3.57", "3.61")
    fix_dh_gnome_clean_for_noble(eds_rules)

    # Keep the newer 3.56 packaging layout (closer to upstream 3.60), while
    # translating only Build-Depends names that Noble cannot resolve.
    adapt_eds_build_deps_for_noble(eds_control)

    # Upstream 3.60 changed these SONAMEs. Do not pretend the new .so files
    # implement Noble's old .so.64/.so.4 ABI package names.
    replace_in_file(eds_control, "libcamel-1.2-64t64", "libcamel-1.2-67t64")
    replace_in_file(eds_control, "libebook-contacts-1.2-4t64", "libebook-contacts-1.2-5t64")

    rename_package_helper_files(eds_debian, "libcamel-1.2-64t64", "libcamel-1.2-67t64")
    rename_package_helper_files(eds_debian, "libebook-contacts-1.2-4t64", "libebook-contacts-1.2-5t64")

    # Old symbols/shlibs manifests encode the old SONAME. Let debhelper derive
    # fresh shlibs metadata for the two new ABI packages instead.
    remove_if_present(eds_debian, "libcamel-1.2-67t64.symbols*")
    remove_if_present(eds_debian, "libcamel-1.2-67t64.shlibs*")
    remove_if_present(eds_debian, "libebook-contacts-1.2-5t64.symbols*")
    remove_if_present(eds_debian, "libebook-contacts-1.2-5t64.shlibs*")

    # Debian carries a tiny patch making this internal helper static. Reproduce
    # the packaging change directly, instead of applying the rest of Debian's
    # 3.56-specific patch series to the newer 3.60 source.
    edbus_cmake = eds_src / "src" / "private" / "CMakeLists.txt"
    edbus_text = edbus_cmake.read_text()
    if "add_library(edbus-private SHARED" in edbus_text:
        edbus_cmake.write_text(edbus_text.replace("add_library(edbus-private SHARED", "add_library(edbus-private STATIC"))
    elif "add_library(edbus-private STATIC" not in edbus_text:
        fail("Could not locate the edbus-private library declaration in EDS 3.60.2.")

    # Debian 3.56.2 symbols files don't match 3.60.2 new symbols; drop them all
    # so debhelper generates fresh ones from the actual built libraries.
    remove_if_present(eds_debian, "*.symbols")

    # evolution-scan-gconf-tree-xml was removed in 3.58+; drop the stale entry
    # from the Debian 3.56 packaging so dh_install doesn't abort on a missing file.
    eds_install = eds_debian / "evolution-data-server.install"
    if eds_install.exists():
        lines = [l for l in eds_install.read_text().splitlines() if "evolution-scan-gconf-tree-xml" not in l]
        eds_install.write_text("\n".join(lines) + "\n")

    add_local_changelog(eds_src, "evolution-data-server")

    print("\n==> EDS Build-Depends after Ubuntu 24.04 compatibility translation:")
    control_lines = eds_control.read_text().splitlines()
    bd_start = next(i for i, l in enumerate(control_lines) if l.startswith("Build-Depends:"))
    bd_end = bd_start + 1
    while bd_end < len(control_lines) and control_lines[bd_end].startswith(" "):
        bd_end += 1
    for line in control_lines[bd_start:bd_end]:
        print(line)

    print("\n==> dh_gnome_clean compatibility:")
    if "dh_gnome_clean --no-control" in eds_rules.read_text():
        print("OK: dh_gnome_clean will run with --no-control")
    else:
        fail("Failed to install dh_gnome_clean --no-control override")

    install_build_deps(eds_src)
    build_component(eds_src, jobs)
    copy_component_debs(eds_root, out_dir)

    print("\n==> Installing freshly built EDS packages needed for the next stage")
    install_component_debs(eds_root)
    run_cmd(["sudo", "ldconfig"])

    # ------------------------------------------------------------------
    # 2. Evolution
    # ------------------------------------------------------------------
    print("\n============================================================")
    print("  2/3  evolution 3.60.2")
    print("============================================================")

    reset_component_dir(evo_root, evo_src)
    unpack_source(evo_tar, evo_debian_tar, evo_src)
    clear_patch_series(evo_src)

    evo_control = evo_src / "debian" / "control"
    evo_rules = evo_src / "debian" / "rules"

    replace_in_file(evo_control, "3.56.2", UPSTREAM_VERSION)
    replace_in_file(evo_control, "3.57", "3.61")
    replace_in_file(evo_rules, "3.56.2", UPSTREAM_VERSION)
    replace_in_file(evo_rules, "3.57", "3.61")
    fix_dh_gnome_clean_for_noble(evo_rules)

    # Noble ships debhelper 13; evolution 3.56.2 packaging uses compat 14.
    replace_in_file(evo_control, "debhelper-compat (= 14)", "debhelper-compat (= 13)")

    # Upstream renamed the appdata file to .metainfo.xml in 3.58+.
    evo_debian = evo_src / "debian"
    evo_install = evo_debian / "evolution.install"
    replace_in_file(evo_install, "org.gnome.Evolution.appdata.xml", "org.gnome.Evolution.metainfo.xml")

    # In 3.60, the evolution private libs are unversioned (.so only, no .so.*).
    # Move them from the .so.* pattern in libevolution.install to just .so,
    # and remove the duplicate glob from evolution-dev.install.
    libevo_install = evo_debian / "libevolution.install"
    evo_dev_install = evo_debian / "evolution-dev.install"
    replace_in_file(libevo_install, "usr/lib/evolution/*.so.*", "usr/lib/evolution/*.so")
    if evo_dev_install.exists():
        lines = [l for l in evo_dev_install.read_text().splitlines() if not l.strip().startswith("usr/lib/evolution/*.so")]
        evo_dev_install.write_text("\n".join(lines) + "\n")

    # The rules file has a stale rm for a file that is now in libevolution, not evolution-dev.
    replace_in_file(
        evo_rules,
        "\trm debian/evolution-dev/usr/lib/evolution/libevolution-rss-common.so",
        "\trm -f debian/evolution-dev/usr/lib/evolution/libevolution-rss-common.so",
    )

    add_local_changelog(evo_src, "evolution")
    install_build_deps(evo_src)
    build_component(evo_src, jobs)
    copy_component_debs(evo_root, out_dir)

    print("\n==> Installing freshly built Evolution packages needed for EWS")
    install_component_debs(evo_root)
    run_cmd(["sudo", "ldconfig"])

    # ------------------------------------------------------------------
    # 3. Evolution-EWS / Microsoft 365
    # ------------------------------------------------------------------
    print("\n============================================================")
    print("  3/3  evolution-ews 3.60.2 (EWS + Microsoft 365)")
    print("============================================================")

    reset_component_dir(ews_root, ews_src)
    unpack_source(ews_tar, ews_debian_tar, ews_src)
    clear_patch_series(ews_src)

    ews_control = ews_src / "debian" / "control"
    ews_rules = ews_src / "debian" / "rules"

    replace_in_file(ews_control, "3.56.2", UPSTREAM_VERSION)
    replace_in_file(ews_control, "3.57", "3.61")
    replace_in_file(ews_rules, "3.56.2", UPSTREAM_VERSION)
    replace_in_file(ews_rules, "3.57", "3.61")
    fix_dh_gnome_clean_for_noble(ews_rules)

    # Noble ships debhelper 13; ews packaging may use compat 14.
    replace_in_file(ews_control, "debhelper-compat (= 14)", "debhelper-compat (= 13)")

    add_local_changelog(ews_src, "evolution-ews")
    install_build_deps(ews_src)
    build_component(ews_src, jobs)
    copy_component_debs(ews_root, out_dir)

    print("\n==> Installing Evolution-EWS / Microsoft 365 packages")
    install_component_debs(ews_root)
    run_cmd(["sudo", "ldconfig"])

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    print("\n============================================================")
    print("  Validation")
    print("============================================================")

    verify_no_usr_local(out_dir)

    evo_version = capture(["evolution", "--version"])
    if UPSTREAM_VERSION not in evo_version:
        fail(f"Installed Evolution does not report {UPSTREAM_VERSION}: {evo_version}")

    m365 = subprocess.run(
        ["find", "/usr/lib", "-type", "f", "-name", "module-microsoft365-configuration.so", "-print", "-quit"],
        capture_output=True, text=True,
    )
    if m365.returncode != 0 or not m365.stdout.strip():
        fail("Evolution-EWS installed, but the Microsoft 365 configuration module was not found.")

    print(f"\nInstalled: {evo_version}")
    print(f"Microsoft 365 module: {m365.stdout.strip()}")

    print("\nInstalled package versions:")
    run_cmd([
        "dpkg-query", "-W",
        "evolution", "evolution-common", "libevolution",
        "evolution-data-server", "evolution-data-server-common",
        "evolution-ews", "evolution-ews-core",
        "libcamel-1.2-67t64", "libebook-contacts-1.2-5t64",
    ])

    print("\nSUCCESS")
    print(f"All generated .deb files are in:\n  {out_dir}")
    print("\nThe main packages are same-name, higher-version replacements for Noble's")
    print("Evolution packages and install under /usr. The old libcamel .so.64 and")
    print("libebook-contacts .so.4 runtime packages are intentionally allowed to")
    print("coexist because 3.60.2 uses the new .so.67/.so.5 ABIs.")
    print("\nLog out/in (or reboot) before using Evolution so any old EDS background")
    print("processes are replaced by the newly installed binaries/libraries.")


def check_up_to_date(args) -> str | None:
    if dpkg_version("evolution") == LOCAL_VERSION:
        return f"Evolution {LOCAL_VERSION} is already installed. Use --force to rebuild."
    return None


def main() -> int:
    return cli.run(
        build=lambda args: build(),
        do_check=lambda args: do_check(),
        check_up_to_date=check_up_to_date,
        description=__doc__,
        install_help="Accepted for interface consistency; Evolution always installs during build",
    )


if __name__ == "__main__":
    sys.exit(main())
