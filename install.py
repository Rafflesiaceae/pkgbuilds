#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


PKGBUILD_ROOT = Path(__file__).resolve().parent
INSTALL_LIST = PKGBUILD_ROOT / "install-list"
INSTALL_LIST_AUR = PKGBUILD_ROOT / "install-list-aur"
AUR_BUILD_ROOT = PKGBUILD_ROOT / ".aur-build"

SEPARATOR = "=" * 64


@dataclass
class Stats:
    processed: int = 0
    outdated: int = 0
    unchecked: int = 0
    failed: int = 0


def heading(label: str) -> None:
    print()
    print(SEPARATOR)
    print(label)
    print(SEPARATOR)


def command(
    args: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    parsed_output: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if parsed_output:
        env["LC_ALL"] = "C"
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )


def show_command_error(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout, end="", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def read_package_list(path: Path) -> list[str]:
    if not path.is_file():
        return []

    entries: list[str] = []
    for raw_line in path.read_text().splitlines():
        entry = raw_line.strip()
        if entry and not entry.startswith("#"):
            entries.append(entry)
    return entries


def valid_entry(entry: str) -> bool:
    return entry not in {".", ".."} and "/" not in entry


def package_info(package: Path) -> tuple[str, str] | None:
    result = command(
        ["pacman", "-Qp", package], capture=True, parsed_output=True
    )
    if result.returncode != 0:
        return None

    fields = result.stdout.strip().split(maxsplit=1)
    if len(fields) != 2:
        return None
    return fields[0], fields[1]


def installed_version(package_name: str) -> str | None:
    result = command(
        ["pacman", "-Q", package_name], capture=True, parsed_output=True
    )
    if result.returncode != 0:
        return None

    fields = result.stdout.strip().split(maxsplit=1)
    return fields[1] if len(fields) == 2 else None


def compare_versions(left: str, right: str) -> int:
    result = command(["vercmp", left, right], capture=True)
    if result.returncode != 0:
        raise RuntimeError(f"vercmp failed for {left!r} and {right!r}")
    return int(result.stdout.strip())


def makepkg_package_list(directory: Path) -> list[Path]:
    result = command(
        ["makepkg", "--packagelist"],
        cwd=directory,
        capture=True,
        extra_env={"PKGDEST": str(directory)},
    )
    if result.returncode != 0:
        show_command_error(result)
        return []
    return [Path(line) for line in result.stdout.splitlines() if line]


def find_built_package(
    wanted: str, packages: Iterable[Path]
) -> Path | None:
    for package in packages:
        if not package.is_file():
            continue
        info = package_info(package)
        if info and info[0] == wanted:
            return package
    return None


def find_cached_aur_package(
    directory: Path, wanted: str, wanted_version: str
) -> Path | None:
    for package in directory.glob("*.pkg.tar.*"):
        if package.name.endswith(".sig") or not package.is_file():
            continue
        info = package_info(package)
        if info == (wanted, wanted_version):
            return package
    return None


def parse_aur_info(output: str) -> tuple[str | None, str | None]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values.get("Version"), values.get("Package Base")


def dependency_name(requirement: str) -> str:
    for operator in "<>=":
        requirement = requirement.split(operator, 1)[0]
    return requirement


def makepkg_dependencies(directory: Path) -> set[str] | None:
    result = command(
        ["makepkg", "--printsrcinfo"], cwd=directory, capture=True
    )
    if result.returncode != 0:
        show_command_error(result)
        return None

    # Architecture-specific dependency fields supplement the generic fields.
    architecture = platform.machine()
    dependency_keys = {
        "depends",
        "makedepends",
        "checkdepends",
        f"depends_{architecture}",
        f"makedepends_{architecture}",
        f"checkdepends_{architecture}",
    }
    dependencies: set[str] = set()
    for line in result.stdout.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if separator and key in dependency_keys:
            dependencies.add(value)
    return dependencies


def aur_build_is_current(
    directory: Path, package: Path, dependencies: Iterable[str]
) -> tuple[bool, str]:
    result = command(
        ["bsdtar", "-xOf", package, ".BUILDINFO"], capture=True
    )
    if result.returncode != 0:
        return False, "package has no readable .BUILDINFO"
    build_info = result.stdout

    built_hash = next(
        (
            line.removeprefix("pkgbuild_sha256sum = ")
            for line in build_info.splitlines()
            if line.startswith("pkgbuild_sha256sum = ")
        ),
        None,
    )
    current_hash = hashlib.sha256(
        (directory / "PKGBUILD").read_bytes()
    ).hexdigest()
    if built_hash != current_hash:
        return False, "PKGBUILD changed"

    built_packages = {
        line.removeprefix("installed = ")
        for line in build_info.splitlines()
        if line.startswith("installed = ")
    }

    result = command(
        ["expac", "-Q", "%n\t%v\t%a\t%S"],
        capture=True,
        parsed_output=True,
    )
    if result.returncode != 0:
        show_command_error(result)
        return False, "could not inspect installed dependencies"

    installed: dict[str, tuple[str, str]] = {}
    providers: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t", 3)
        if len(fields) < 3:
            continue
        name, version, package_arch = fields[:3]
        provides = fields[3] if len(fields) == 4 else ""
        installed[name] = (version, package_arch)
        for provided in provides.split():
            providers.setdefault(dependency_name(provided), name)

    for requirement in sorted(dependencies):
        name = dependency_name(requirement)
        resolved_name = name if name in installed else providers.get(name)
        if resolved_name is None:
            return False, f"dependency {name} is not installed"

        version, package_arch = installed[resolved_name]
        expected = f"{resolved_name}-{version}-{package_arch}"
        if expected not in built_packages:
            return False, f"dependency {resolved_name} changed"

    return True, ""


class Installer:
    def __init__(self, check_only: bool) -> None:
        self.check_only = check_only
        self.stats = Stats()
        self.install_packages: list[Path] = []
        self.force_install_packages: list[Path] = []

    def fail(self, message: str) -> None:
        print(f"ERROR: {message}", file=sys.stderr)
        self.stats.failed += 1

    def queue_package(
        self, wanted: str, packages: Iterable[Path], *, force: bool = False
    ) -> None:
        packages = list(packages)
        package = find_built_package(wanted, packages)
        if package is None:
            self.fail(f"Build produced no package named {wanted}")
            return

        queue = (
            self.force_install_packages if force else self.install_packages
        )
        if package not in queue:
            queue.append(package)
            print(f"==> Queued {package.name} for installation")
        self.stats.processed += 1

    def process_local(self, entry: str) -> None:
        heading(f"==> LOCAL: {entry}")
        directory = PKGBUILD_ROOT / entry

        if not directory.is_dir():
            self.fail(f"Directory does not exist: {directory}")
            return
        if not (directory / "PKGBUILD").is_file():
            self.fail(f"No PKGBUILD found in: {directory}")
            return

        nvchecker = directory / ".nvchecker.toml"
        if nvchecker.is_file():
            print("==> Checking upstream version...")
            result = command(["pkgctl", "version", "check"], cwd=directory)

            if result.returncode == 0:
                print(f"==> {entry} is up to date.")
                if self.check_only:
                    return
            elif result.returncode == 2:
                print(f"==> {entry} has an update available.")
                self.stats.outdated += 1
                if self.check_only:
                    return

                print("==> Updating PKGBUILD...")
                result = command(
                    ["pkgctl", "version", "upgrade"], cwd=directory
                )
                if result.returncode != 0:
                    self.fail(f"pkgctl version upgrade failed for {entry}")
                    return
            else:
                self.fail(
                    f"Version check failed for {entry} "
                    f"(exit code {result.returncode})"
                )
                return
        elif self.check_only:
            print("==> UNCHECKED: no .nvchecker.toml")
            self.stats.unchecked += 1
            return
        else:
            print("==> No .nvchecker.toml; skipping version check.")
            print("==> Ensuring current PKGBUILD is built.")

        print(f"==> Building/checking {entry}...")
        packages = makepkg_package_list(directory)
        if not packages:
            self.fail(f"makepkg produced no package list for {entry}")
            return

        if find_built_package(entry, packages) is None:
            result = command(
                ["makepkg", "-fsc", "--noconfirm"],
                cwd=directory,
                extra_env={"PKGDEST": str(directory)},
            )
            if result.returncode != 0:
                self.fail(f"Build failed for {entry}")
                return
        else:
            print("==> Reusing existing package file(s).")

        self.queue_package(entry, packages)

    def process_aur(self, entry: str) -> None:
        heading(f"==> AUR: {entry}")
        print("==> Checking AUR version...")

        result = command(
            ["yay", "-Si", "--aur", "--color", "never", entry],
            capture=True,
            parsed_output=True,
        )
        if result.returncode != 0:
            show_command_error(result)
            self.fail(f"Could not query AUR package: {entry}")
            return

        aur_version, aur_pkgbase = parse_aur_info(result.stdout)
        aur_pkgbase = aur_pkgbase or entry
        if not aur_version:
            self.fail(f"Could not determine AUR version for {entry}")
            return

        current_version = installed_version(entry)
        comparison = 0
        if current_version is None:
            print(f"==> {entry} is not installed.")
            print(f"    AUR version: {aur_version}")
            self.stats.outdated += 1
        else:
            comparison = compare_versions(aur_version, current_version)
            if comparison > 0:
                print("==> Update available:")
                print(f"    installed: {current_version}")
                print(f"    AUR:       {aur_version}")
                self.stats.outdated += 1
            elif comparison == 0:
                print(f"==> {entry} is up to date ({aur_version}).")
            else:
                print("==> Installed package is newer than AUR:")
                print(f"    installed: {current_version}")
                print(f"    AUR:       {aur_version}")

        if self.check_only:
            return
        if current_version is not None and comparison < 0:
            return

        directory = AUR_BUILD_ROOT / aur_pkgbase
        if (directory / ".git").is_dir():
            print("==> Updating cached PKGBUILD from AUR...")
            result = command(["git", "-C", directory, "pull", "--ff-only"])
            if result.returncode != 0:
                self.fail(f"Could not update AUR checkout for {entry}")
                return
        else:
            print(f"==> Downloading {entry} from AUR...")
            if directory.exists():
                shutil.rmtree(directory)
            result = command(
                ["yay", "-G", "--aur", entry], cwd=AUR_BUILD_ROOT
            )
            if result.returncode != 0:
                self.fail(f"yay failed to download {entry}")
                return

        if not (directory / "PKGBUILD").is_file():
            self.fail(
                f"Expected downloaded PKGBUILD at {directory / 'PKGBUILD'}"
            )
            return

        dependencies = makepkg_dependencies(directory)
        if dependencies is None:
            self.fail(f"Could not read dependency metadata for {entry}")
            return

        if dependencies:
            print("==> Updating AUR package dependencies...")
            # makepkg only installs missing dependencies, whereas yay also
            # upgrades installed AUR dependencies named as explicit targets.
            dependency_targets = sorted(
                {dependency_name(item) for item in dependencies}
            )
            result = command(
                [
                    "yay",
                    "-S",
                    "--needed",
                    "--asdeps",
                    "--noconfirm",
                    "--",
                    *dependency_targets,
                ]
            )
            if result.returncode != 0:
                self.fail(f"Could not update dependencies for {entry}")
                return

        package = find_cached_aur_package(directory, entry, aur_version)
        if package is not None:
            current, reason = aur_build_is_current(
                directory, package, dependencies
            )
            if current:
                print(
                    "==> Reusing cached build; PKGBUILD and dependencies "
                    "are unchanged."
                )
                self.queue_package(entry, [package])
                return
        else:
            reason = "package archive is missing"

        print(f"==> Rebuilding {entry}: {reason}.")
        result = command(
            ["makepkg", "-fsc", "--noconfirm"],
            cwd=directory,
            extra_env={"PKGDEST": str(directory)},
        )
        if result.returncode != 0:
            self.fail(f"Build failed for {entry}")
            return

        packages = makepkg_package_list(directory)
        if not packages:
            self.fail(f"makepkg produced no package list for AUR package {entry}")
            return

        package = find_built_package(entry, packages)
        if package is None:
            self.fail(f"Build produced no package named {entry}")
            return

        info = package_info(package)
        force = bool(info and current_version == info[1])
        self.queue_package(entry, packages, force=force)

    def run(self) -> int:
        if not INSTALL_LIST.is_file() and not INSTALL_LIST_AUR.is_file():
            print(
                "ERROR: Neither install-list nor install-list-aur exists.",
                file=sys.stderr,
            )
            return 1

        if not self.check_only:
            AUR_BUILD_ROOT.mkdir(parents=True, exist_ok=True)

        for entry in read_package_list(INSTALL_LIST):
            if not valid_entry(entry):
                self.fail(f"Invalid install-list entry: {entry}")
                continue
            self.process_local(entry)

        for entry in read_package_list(INSTALL_LIST_AUR):
            if not valid_entry(entry):
                self.fail(f"Invalid install-list-aur entry: {entry}")
                continue
            self.process_aur(entry)

        if self.check_only:
            heading("Check summary")
            print(f"Needs update/build: {self.stats.outdated}")
            print(f"Unchecked:          {self.stats.unchecked}")
            print(f"Failed:             {self.stats.failed}")
            print(SEPARATOR)
            if self.stats.failed:
                return 1
            return 2 if self.stats.outdated else 0

        if self.install_packages:
            heading("==> Installing requested local packages")
            result = command(
                [
                    "sudo",
                    "pacman",
                    "-U",
                    "--needed",
                    "--",
                    *self.install_packages,
                ]
            )
            if result.returncode != 0:
                print(
                    "ERROR: Installing local packages failed", file=sys.stderr
                )
                return 1

        if self.force_install_packages:
            heading("==> Reinstalling rebuilt packages")
            result = command(
                ["sudo", "pacman", "-U", "--", *self.force_install_packages]
            )
            if result.returncode != 0:
                print(
                    "ERROR: Reinstalling rebuilt packages failed",
                    file=sys.stderr,
                )
                return 1

        if not self.install_packages and not self.force_install_packages:
            print("\n==> Nothing needs installing.")

        print()
        print(SEPARATOR)
        print(f"Processed: {self.stats.processed}")
        print(f"Failed:    {self.stats.failed}")
        print(SEPARATOR)
        return 1 if self.stats.failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="check for updates without building or installing packages",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return Installer(check_only=args.check).run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except FileNotFoundError as error:
        print(
            f"ERROR: Required command not found: {error.filename}",
            file=sys.stderr,
        )
        raise SystemExit(1)
