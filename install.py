#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


PKGBUILD_ROOT = Path(__file__).resolve().parent
INSTALL_LIST = PKGBUILD_ROOT / "install-list"
INSTALL_LIST_AUR = PKGBUILD_ROOT / "install-list-aur"
AUR_BUILD_ROOT = PKGBUILD_ROOT / ".aur-build"

SEPARATOR = "=" * 64

# Default worker count for the parallel check/build phase (-j/--jobs).
DEFAULT_JOBS = 4

# pacman/apt/dpkg each hold a single system-wide database lock, so running
# more than one package-manager mutation at a time doesn't parallelize
# anything -- it just makes the losing process fail with a lock-contention
# error. Every command that can invoke pacman/apt as root (makepkg -s, yay
# -S, ubuntu24.py --install) is funneled through this lock so those steps
# still run one at a time even though everything else (version checks,
# downloads, plain `makepkg --packagelist`/`--printsrcinfo`) runs freely
# across worker threads.
PACMAN_LOCK = threading.Lock()


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


COLOR = _supports_color()


def _code(escape: str) -> str:
    return escape if COLOR else ""


RESET = _code("\033[0m")
BOLD = _code("\033[1m")
DIM = _code("\033[2m")
RED = _code("\033[31m")
GREEN = _code("\033[32m")
YELLOW = _code("\033[33m")
CYAN = _code("\033[36m")


def colored(text: str, *codes: str) -> str:
    if not COLOR or not codes:
        return text
    return f"{''.join(codes)}{text}{RESET}"


def count(value: int, warn_color: str) -> str:
    """Render a summary count, colored when non-zero (e.g. any failure in red)."""
    return colored(str(value), warn_color) if value else str(value)


def detect_os() -> tuple[str, str]:
    """Return (os_id, version_id) from /etc/os-release, or ('', '') on failure."""
    try:
        # freedesktop_os_release is available in Python 3.10+.
        info = platform.freedesktop_os_release()
        return info.get("ID", ""), info.get("VERSION_ID", "")
    except (AttributeError, OSError):
        return "", ""


def is_ubuntu_24() -> bool:
    """Return True when running on Ubuntu 24.x."""
    os_id, version_id = detect_os()
    return os_id == "ubuntu" and version_id.startswith("24.")


@dataclass
class Stats:
    processed: int = 0
    outdated: int = 0
    unchecked: int = 0
    failed: int = 0
    # Only used by the Ubuntu 24.x install flow, which distinguishes a no-op
    # check from an actual rebuild (see lib/cli.py's run() exit codes).
    up_to_date: int = 0
    rebuilt: int = 0


def heading(label: str) -> None:
    print()
    print(colored(SEPARATOR, DIM))
    print(colored(label, BOLD))
    print(colored(SEPARATOR, DIM))


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


def read_package_list(path: Path) -> list[str]:
    if not path.is_file():
        return []

    entries: list[str] = []
    for raw_line in path.read_text().splitlines():
        # Strip inline comments before trimming whitespace.
        entry = raw_line.partition("#")[0].strip()
        if entry:
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


VERBOSE_AFTER_SECONDS = 60.0

# Status labels can contain the color sequences also used in the buffered
# task log. They must not count toward the terminal width or be cut in half.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def terminal_text_width(text: str) -> int:
    """Return the number of terminal cells occupied by plain text."""
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        # Terminal control characters occupy no cells. Status text should not
        # normally contain them, but treating them as zero keeps this helper
        # correct for sanitized command output as well.
        if unicodedata.category(character).startswith("C"):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def fit_terminal_line(text: str, columns: int) -> str:
    """Trim text so an in-place status line cannot wrap in the terminal."""
    # Leave the last column unused. Some terminals enter pending-wrap state
    # when it is filled, making a later carriage return act inconsistently.
    available = max(0, columns - 1)
    if terminal_text_width(text) <= available:
        return text
    if available == 0:
        return ""

    ellipsis = "…"
    content_width = available - terminal_text_width(ellipsis)
    width = 0
    result: list[str] = []
    for character in text:
        character_width = terminal_text_width(character)
        if width + character_width > content_width:
            break
        result.append(character)
        width += character_width
    return "".join(result) + ellipsis


class StatusBoard:
    """Thread-safe live status display for concurrently running tasks.

    Worker threads never touch the real terminal directly -- they buffer
    output in a Task and only report a short status label here. When
    attached to a terminal, active tasks are summarized on a single
    trailing spinner line; finished tasks are printed as a normal line
    that scrolls up, after which the spinner line is redrawn below it.
    When not attached to a terminal (piped/redirected output), the
    spinner is skipped and only start/finish lines are printed.

    Deliberately a *single* status line rather than one line per task:
    an earlier version moved the cursor up over a multi-line block with
    `\\033[nA` and redrew it in place, which relies on the previously
    drawn block still being exactly N rows above the cursor. The first
    time the block grows (typically right at startup, when the shell
    prompt already sits near the bottom of the terminal) that growth can
    trigger a scroll, which shifts everything up a row and desyncs that
    cursor-relative math -- seen as flicker, worst right at the start.
    A single line never needs multi-row movement (just `\\r` within the
    current line), so it can't hit that failure mode at all.

    Any task still running after VERBOSE_AFTER_SECONDS is "promoted": its
    buffered output (so far, and from then on) is streamed live -- prefixed
    with its label -- instead of staying hidden behind the status line, so
    a genuinely slow/stuck step is visible instead of looking identical to
    a fast one.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    INTERVAL = 0.1

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._tasks: dict[str, "Task"] = {}
        self._frame = 0
        self._line_drawn = False
        self._suspended = False
        self._pending_finished: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.enabled:
            # Hide the cursor for the duration of the animation: leaving it
            # visible at the redraw point reads as extra blinking on top of
            # the spinner itself on some terminals (e.g. rxvt-unicode).
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
        # The tick loop also drives the >60s "show full output" promotion,
        # so it runs regardless of whether the spinner itself is enabled.
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        with self._lock:
            out = self._clear_line()
            if self.enabled:
                out += "\033[?25h"
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()

    def add_task(self, key: str, label: str, task: "Task") -> None:
        with self._lock:
            self._order.append(key)
            self._tasks[key] = task
            if not self.enabled:
                sys.stdout.write(f"{DIM}==>{RESET} Started: {label}\n")
                sys.stdout.flush()

    def suspend(self) -> None:
        """Clear the spinner and expose the terminal for an interactive prompt."""
        if not self.enabled:
            return
        with self._lock:
            if self._suspended:
                return
            self._suspended = True
            # Move prompt output to a fresh row so it cannot overwrite the
            # spinner that was just cleared. The cursor is shown for password
            # input and other prompts that rely on normal terminal feedback.
            out = self._clear_line()
            if out:
                out += "\n"
            out += "\033[?25h"
            sys.stdout.write(out)
            sys.stdout.flush()

    def resume(self) -> None:
        """Restore the spinner after an interactive command has completed."""
        if not self.enabled:
            return
        with self._lock:
            if not self._suspended:
                return
            self._suspended = False
            # Completions from other workers are held until the prompt is
            # finished, otherwise their normal status lines would interleave
            # with the password prompt.
            out = "".join(self._pending_finished)
            self._pending_finished.clear()
            out += "\033[?25l"
            out += self._status_line()
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()

    def finish_task(self, key: str, summary_line: str, full_log: str | None) -> None:
        with self._lock:
            if key in self._order:
                self._order.remove(key)
            self._tasks.pop(key, None)
            if self._suspended:
                # `suspend()` already cleared the live line, so retain only
                # the completed task output for `resume()` to print later.
                out = summary_line + "\n"
                if full_log is not None:
                    out += full_log + "\n"
                self._pending_finished.append(out)
                return
            # Build the whole clear+print+redraw update as one string and
            # emit it in a single write()+flush(): issuing several writes in
            # a row (each ending in a newline, which auto-flushes a
            # line-buffered TTY stream) makes the terminal render the clear
            # and the redraw as separate visible frames, i.e. a flicker.
            out = self._clear_line() if self.enabled else ""
            out += summary_line + "\n"
            if full_log is not None:
                out += full_log + "\n"
            if self.enabled:
                out += self._status_line()
            sys.stdout.write(out)
            sys.stdout.flush()

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            with self._lock:
                if self._suspended:
                    continue
                self._frame += 1
                inserted = ""
                for key in self._order:
                    task = self._tasks[key]
                    if not task.verbose and task.elapsed() > VERBOSE_AFTER_SECONDS:
                        task.verbose = True
                        inserted += task.pop_pending_output(
                            note=colored(
                                f"--- {task.label} is taking a while; showing live output ---",
                                DIM,
                            )
                        )
                    elif task.verbose:
                        inserted += task.pop_pending_output()

                if inserted:
                    # New lines have to be inserted above the status line,
                    # which reflows it anyway, so a full clear+rebuild here
                    # is unavoidable -- but this only happens once per
                    # promotion or new output, not on every animation tick.
                    out = (self._clear_line() if self.enabled else "") + inserted
                    out += self._status_line() if self.enabled else ""
                else:
                    out = self._update_line() if self.enabled else ""

                if out:
                    sys.stdout.write(out)
                    sys.stdout.flush()

    def _status_text(self) -> str:
        if not self._order:
            return ""
        frame = self.FRAMES[self._frame % len(self.FRAMES)]
        if len(self._order) == 1:
            detail = self._tasks[self._order[0]].status_label
        else:
            detail = ", ".join(self._tasks[key].label for key in self._order)
        # A carriage return only moves to the start of the current physical
        # row. If this text wraps, subsequent frames are therefore redrawn on
        # the wrapped row and accumulate instead of replacing one another.
        plain_text = ANSI_ESCAPE.sub(
            "", f"{frame} {len(self._order)} running: {detail}"
        )
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        fitted = fit_terminal_line(plain_text, columns)
        if not fitted:
            return ""
        # Only the first character is the spinner frame; applying its color
        # after fitting keeps escape sequences out of the width calculation.
        return f"{CYAN}{fitted[0]}{RESET}{fitted[1:]}"

    def _status_line(self) -> str:
        """Fresh draw of the status line (cursor already at column 0)."""
        text = self._status_text()
        self._line_drawn = bool(text)
        return f"{text}\033[K" if text else ""

    def _update_line(self) -> str:
        """In-place redraw: write new text, then trim leftovers -- never
        erase before there is already new content on the line, since an
        erase-then-redraw leaves a visible blank frame on terminals that
        don't coalesce the two into one screen update."""
        text = self._status_text()
        if not text:
            return self._clear_line()
        self._line_drawn = True
        return f"\r{text}\033[K"

    def _clear_line(self) -> str:
        if not self._line_drawn:
            return ""
        self._line_drawn = False
        return "\r\033[K"


class Task:
    """Per-entry execution context and outcome accumulator.

    Runs inside a worker thread. All command output and progress messages
    are buffered in `lines` instead of going to the real terminal, so
    concurrent workers never interleave output. The buffered log is shown
    in full when the task fails, or streamed live (see StatusBoard) once
    the task has been running for more than VERBOSE_AFTER_SECONDS.
    """

    def __init__(self, board: StatusBoard, label: str):
        self.board = board
        self.label = label
        self.status_label = label
        self.key = f"{id(self)}:{label}"
        self.lines: list[str] = [label]
        self.ok = True
        self.processed = 0
        self.outdated = 0
        self.unchecked = 0
        self.up_to_date = 0
        self.rebuilt = 0
        self.queued: Path | None = None
        self.queued_force: Path | None = None
        self.errors: list[str] = []
        self.started = time.monotonic()
        # Guards `lines`/`_flushed` against the board's tick thread reading
        # them concurrently while this task's worker thread appends to them.
        self._lock = threading.Lock()
        self.verbose = False
        self._flushed = 0
        board.add_task(self.key, label, self)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def log(self, text: str = "") -> None:
        with self._lock:
            self.lines.append(text)
        if text.strip():
            self.status_label = text.strip()

    def pop_pending_output(self, note: str | None = None) -> str:
        """Return (and mark shown) any buffered lines not yet streamed live,
        each prefixed with this task's label so concurrent verbose tasks
        stay distinguishable."""
        with self._lock:
            chunk = self.lines[self._flushed:]
            self._flushed = len(self.lines)
        prefix = colored(f"[{self.label}]", DIM) + " "
        out = f"{note}\n" if note else ""
        out += "".join(f"{prefix}{line}\n" for line in chunk)
        return out

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)
        self.log(f"{RED}ERROR:{RESET} {message}")

    def run(
        self,
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        parsed_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.log(f"{DIM}${RESET} {' '.join(str(a) for a in args)}")
        result = command(
            args, cwd=cwd, capture=True, parsed_output=parsed_output, extra_env=extra_env
        )
        for stream in (result.stdout, result.stderr):
            if stream:
                with self._lock:
                    self.lines.extend(stream.rstrip("\n").splitlines())
        return result

    def run_exclusive(
        self,
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        parsed_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Like run(), but serialized against other package-manager mutations."""
        self.status_label = "waiting for package-manager lock..."
        with PACMAN_LOCK:
            self.status_label = f"running: {' '.join(str(a) for a in args)}"
            if sys.stdin.isatty():
                # A non-interactive probe avoids disturbing an already valid
                # sudo timestamp. When it fails, run the real validation with
                # the terminal attached while the spinner is suspended so any
                # password prompt remains visible and usable.
                auth = command(["sudo", "-n", "-v"], capture=True)
                if auth.returncode != 0:
                    self.board.suspend()
                    try:
                        auth = command(["sudo", "-v"])
                    finally:
                        self.board.resume()
                    if auth.returncode != 0:
                        return auth
            return self.run(args, cwd=cwd, extra_env=extra_env, parsed_output=parsed_output)

    def queue(self, wanted: str, packages: Iterable[Path], *, force: bool = False) -> None:
        packages = list(packages)
        package = find_built_package(wanted, packages)
        if package is None:
            self.fail(f"Build produced no package named {wanted}")
            return

        if force:
            self.queued_force = package
        else:
            self.queued = package
        self.log(f"Queued {package.name} for installation")
        self.processed = 1

    def summary(self) -> str:
        if not self.ok:
            return colored("; ".join(self.errors) or "failed", RED)

        bits = []
        if self.up_to_date:
            bits.append("up to date")
        if self.rebuilt:
            bits.append(colored("rebuilt", GREEN))
        if self.outdated:
            bits.append(colored("update available", YELLOW))
        if self.unchecked:
            bits.append(colored("unchecked", DIM))
        if self.queued:
            bits.append(colored(f"queued {self.queued.name}", GREEN))
        if self.queued_force:
            bits.append(colored(f"queued {self.queued_force.name} (reinstall)", GREEN))
        return ", ".join(bits) or "up to date"

    def finish(self) -> None:
        icon = colored("✔", GREEN) if self.ok else colored("✖", RED)
        line = f"{icon} {self.label}  {DIM}({self.elapsed():0.1f}s){RESET}  {self.summary()}"
        if self.verbose:
            # Already streamed live; just flush whatever hasn't been shown yet.
            full_log = self.pop_pending_output().rstrip("\n") or None
        elif not self.ok:
            full_log = "\n".join(self.lines)
        else:
            full_log = None
        self.board.finish_task(self.key, line, full_log)


def makepkg_package_list(directory: Path, task: Task) -> list[Path]:
    result = task.run(
        ["makepkg", "--packagelist"], cwd=directory, extra_env={"PKGDEST": str(directory)}
    )
    if result.returncode != 0:
        return []
    return [Path(line) for line in result.stdout.splitlines() if line]


def makepkg_dependencies(directory: Path, task: Task) -> set[str] | None:
    result = task.run(["makepkg", "--printsrcinfo"], cwd=directory)
    if result.returncode != 0:
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
    directory: Path, package: Path, dependencies: Iterable[str], task: Task
) -> tuple[bool, str]:
    result = task.run(["bsdtar", "-xOf", package, ".BUILDINFO"])
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

    result = task.run(
        ["expac", "-Q", "%n\t%v\t%a\t%S"],
        parsed_output=True,
    )
    if result.returncode != 0:
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


def process_ubuntu24(entry: str, task: Task, check_only: bool) -> None:
    """Check, then (if needed) install <entry>/ubuntu24.py for an
    Ubuntu 24.x system.

    In the non-check-only flow, ubuntu24.py's own --install already runs
    check_up_to_date() first and reports which branch it took via exit
    code (see lib/cli.py), so we don't need a separate --check pass here.
    """
    directory = PKGBUILD_ROOT / entry

    if not directory.is_dir():
        task.fail(f"Directory does not exist: {directory}")
        return

    script = directory / "ubuntu24.py"
    if not script.is_file():
        # Treat packages without an ubuntu24.py as unchecked/unsupported.
        task.log(f"No ubuntu24.py found in {entry}; skipping.")
        task.unchecked = 1
        return

    if check_only:
        task.log(f"Running ubuntu24.py --check for {entry}...")
        result = task.run(["python", script, "--check"], cwd=directory)
        # ubuntu24.py's --check exits 0 when up to date, 1 when a build
        # is needed; any other code means the check itself broke.
        if result.returncode == 0:
            pass
        elif result.returncode == 1:
            task.outdated = 1
        else:
            task.fail(
                f"ubuntu24.py --check failed for {entry} "
                f"(exit code {result.returncode})"
            )
        return

    task.log(f"Running ubuntu24.py --install for {entry}...")
    # Installing may run apt-get under the hood, so this goes through the
    # shared package-manager lock.
    result = task.run_exclusive(["python", script, "--install"], cwd=directory)
    # Exit code 2 means check_up_to_date() skipped the build (already
    # current); 0 means it actually built/installed; anything else failed.
    if result.returncode == 2:
        task.up_to_date = 1
    elif result.returncode == 0:
        task.rebuilt = 1
    else:
        task.fail(f"ubuntu24.py failed for {entry}")
        return

    task.processed = 1


def process_local(entry: str, task: Task, check_only: bool) -> None:
    directory = PKGBUILD_ROOT / entry

    if not directory.is_dir():
        task.fail(f"Directory does not exist: {directory}")
        return
    if not (directory / "PKGBUILD").is_file():
        task.fail(f"No PKGBUILD found in: {directory}")
        return

    nvchecker = directory / ".nvchecker.toml"
    if nvchecker.is_file():
        task.log("Checking upstream version...")
        result = task.run(["pkgctl", "version", "check"], cwd=directory)

        if result.returncode == 0:
            task.log(f"{entry} is up to date.")
            if check_only:
                return
        elif result.returncode == 2:
            task.log(f"{entry} has an update available.")
            task.outdated = 1
            if check_only:
                return

            task.log("Updating PKGBUILD...")
            result = task.run(["pkgctl", "version", "upgrade"], cwd=directory)
            if result.returncode != 0:
                task.fail(f"pkgctl version upgrade failed for {entry}")
                return
        else:
            task.fail(
                f"Version check failed for {entry} "
                f"(exit code {result.returncode})"
            )
            return
    elif check_only:
        task.log("UNCHECKED: no .nvchecker.toml")
        task.unchecked = 1
        return
    else:
        task.log("No .nvchecker.toml; skipping version check.")
        task.log("Ensuring current PKGBUILD is built.")

    task.log(f"Building/checking {entry}...")
    packages = makepkg_package_list(directory, task)
    if not packages:
        task.fail(f"makepkg produced no package list for {entry}")
        return

    if find_built_package(entry, packages) is None:
        # -s (syncdeps) means makepkg may call `sudo pacman -S` for missing
        # build/check deps, so this must go through the shared lock.
        result = task.run_exclusive(
            ["makepkg", "-fsc", "--noconfirm"],
            cwd=directory,
            extra_env={"PKGDEST": str(directory)},
        )
        if result.returncode != 0:
            task.fail(f"Build failed for {entry}")
            return
    else:
        task.log("Reusing existing package file(s).")

    task.queue(entry, packages)


def process_aur(entry: str, task: Task, check_only: bool) -> None:
    task.log("Checking AUR version...")

    result = task.run(
        ["yay", "-Si", "--aur", "--color", "never", entry], parsed_output=True
    )
    if result.returncode != 0:
        task.fail(f"Could not query AUR package: {entry}")
        return

    aur_version, aur_pkgbase = parse_aur_info(result.stdout)
    aur_pkgbase = aur_pkgbase or entry
    if not aur_version:
        task.fail(f"Could not determine AUR version for {entry}")
        return

    current_version = installed_version(entry)
    comparison = 0
    if current_version is None:
        task.log(f"{entry} is not installed.")
        task.log(f"AUR version: {aur_version}")
        task.outdated = 1
    else:
        try:
            comparison = compare_versions(aur_version, current_version)
        except RuntimeError as error:
            task.fail(str(error))
            return
        if comparison > 0:
            task.log("Update available:")
            task.log(f"installed: {current_version}")
            task.log(f"AUR:       {aur_version}")
            task.outdated = 1
        elif comparison == 0:
            task.log(f"{entry} is up to date ({aur_version}).")
        else:
            task.log("Installed package is newer than AUR:")
            task.log(f"installed: {current_version}")
            task.log(f"AUR:       {aur_version}")

    if check_only:
        return
    if current_version is not None and comparison < 0:
        return

    directory = AUR_BUILD_ROOT / aur_pkgbase
    if (directory / ".git").is_dir():
        task.log("Updating cached PKGBUILD from AUR...")
        result = task.run(["git", "-C", directory, "pull", "--ff-only"])
        if result.returncode != 0:
            task.fail(f"Could not update AUR checkout for {entry}")
            return
    else:
        task.log(f"Downloading {entry} from AUR...")
        if directory.exists():
            shutil.rmtree(directory)
        result = task.run(["yay", "-G", "--aur", entry], cwd=AUR_BUILD_ROOT)
        if result.returncode != 0:
            task.fail(f"yay failed to download {entry}")
            return

    if not (directory / "PKGBUILD").is_file():
        task.fail(
            f"Expected downloaded PKGBUILD at {directory / 'PKGBUILD'}"
        )
        return

    dependencies = makepkg_dependencies(directory, task)
    if dependencies is None:
        task.fail(f"Could not read dependency metadata for {entry}")
        return

    if dependencies:
        task.log("Updating AUR package dependencies...")
        # makepkg only installs missing dependencies, whereas yay also
        # upgrades installed AUR dependencies named as explicit targets.
        dependency_targets = sorted(
            {dependency_name(item) for item in dependencies}
        )
        result = task.run_exclusive(
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
            task.fail(f"Could not update dependencies for {entry}")
            return

    package = find_cached_aur_package(directory, entry, aur_version)
    if package is not None:
        current, reason = aur_build_is_current(directory, package, dependencies, task)
        if current:
            task.log(
                "Reusing cached build; PKGBUILD and dependencies are unchanged."
            )
            task.queue(entry, [package])
            return
    else:
        reason = "package archive is missing"

    task.log(f"Rebuilding {entry}: {reason}.")
    result = task.run_exclusive(
        ["makepkg", "-fsc", "--noconfirm"],
        cwd=directory,
        extra_env={"PKGDEST": str(directory)},
    )
    if result.returncode != 0:
        task.fail(f"Build failed for {entry}")
        return

    packages = makepkg_package_list(directory, task)
    if not packages:
        task.fail(f"makepkg produced no package list for AUR package {entry}")
        return

    package = find_built_package(entry, packages)
    if package is None:
        task.fail(f"Build produced no package named {entry}")
        return

    info = package_info(package)
    force = bool(info and current_version == info[1])
    task.queue(entry, packages, force=force)


def run_task(kind: str, entry: str, board: StatusBoard, check_only: bool) -> Task:
    """Dispatch a single install-list entry to its processing function.

    Runs in a worker thread. Any uncaught exception (e.g. a vercmp parsing
    error) is turned into a normal task failure instead of crashing the
    whole run and losing every other in-flight/queued package.
    """
    label = f"{kind.upper()}: {entry}"
    task = Task(board, label)
    try:
        if kind == "local":
            process_local(entry, task, check_only)
        elif kind == "aur":
            process_aur(entry, task, check_only)
        else:
            process_ubuntu24(entry, task, check_only)
    except Exception:
        task.fail("Unexpected error")
        task.log(traceback.format_exc().rstrip())
    task.finish()
    return task


class Installer:
    def __init__(self, check_only: bool, jobs: int) -> None:
        self.check_only = check_only
        self.jobs = jobs
        self.stats = Stats()
        self.install_packages: list[Path] = []
        self.force_install_packages: list[Path] = []

    def _run_jobs(self, jobs: list[tuple[str, str]]) -> None:
        """Process entries concurrently, folding each finished Task's
        outcome into self.stats/self.install_packages as it completes.

        Only this (main) thread touches self.stats and the install-queue
        lists, so no additional locking is needed for them.
        """
        if not jobs:
            return

        board = StatusBoard(enabled=sys.stdout.isatty())
        board.start()
        try:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                futures = [
                    pool.submit(run_task, kind, entry, board, self.check_only)
                    for kind, entry in jobs
                ]
                for future in as_completed(futures):
                    task = future.result()
                    self.stats.processed += task.processed
                    self.stats.outdated += task.outdated
                    self.stats.unchecked += task.unchecked
                    self.stats.up_to_date += task.up_to_date
                    self.stats.rebuilt += task.rebuilt
                    if not task.ok:
                        self.stats.failed += 1
                    if task.queued:
                        self.install_packages.append(task.queued)
                    if task.queued_force:
                        self.force_install_packages.append(task.queued_force)
        finally:
            board.stop()

    def run(self) -> int:
        if not INSTALL_LIST.is_file() and not INSTALL_LIST_AUR.is_file():
            print(
                f"{RED}ERROR: Neither install-list nor install-list-aur exists.{RESET}",
                file=sys.stderr,
            )
            return 1

        # Dispatch to the Ubuntu 24.x flow when running on that distro.
        if is_ubuntu_24():
            return self._run_ubuntu24()

        return self._run_arch()

    def _run_ubuntu24(self) -> int:
        """Process all install-list entries via their ubuntu24.py scripts."""
        jobs: list[tuple[str, str]] = []
        for entry in read_package_list(INSTALL_LIST):
            if not valid_entry(entry):
                print(f"{RED}ERROR: Invalid install-list entry: {entry}{RESET}", file=sys.stderr)
                self.stats.failed += 1
                continue
            jobs.append(("ubuntu24", entry))

        self._run_jobs(jobs)

        if self.check_only:
            heading("Check summary (Ubuntu 24)")
            print(f"Needs update/build: {count(self.stats.outdated, YELLOW)}")
            print(f"Unchecked:          {self.stats.unchecked}")
            print(f"Failed:             {count(self.stats.failed, RED)}")
            print(colored(SEPARATOR, DIM))
            if self.stats.failed:
                return 1
            return 2 if self.stats.outdated else 0

        print()
        print(colored(SEPARATOR, DIM))
        print(f"Processed:  {self.stats.processed}")
        print(f"Up to date: {self.stats.up_to_date}")
        print(f"Rebuilt:    {colored(str(self.stats.rebuilt), GREEN) if self.stats.rebuilt else self.stats.rebuilt}")
        print(f"Skipped:    {self.stats.unchecked}")
        print(f"Failed:     {count(self.stats.failed, RED)}")
        print(colored(SEPARATOR, DIM))
        return 1 if self.stats.failed else 0

    def _run_arch(self) -> int:
        """Process all install-list and install-list-aur entries via pacman/AUR."""
        if not self.check_only:
            AUR_BUILD_ROOT.mkdir(parents=True, exist_ok=True)

        jobs: list[tuple[str, str]] = []
        for entry in read_package_list(INSTALL_LIST):
            if not valid_entry(entry):
                print(f"{RED}ERROR: Invalid install-list entry: {entry}{RESET}", file=sys.stderr)
                self.stats.failed += 1
                continue
            jobs.append(("local", entry))

        for entry in read_package_list(INSTALL_LIST_AUR):
            if not valid_entry(entry):
                print(f"{RED}ERROR: Invalid install-list-aur entry: {entry}{RESET}", file=sys.stderr)
                self.stats.failed += 1
                continue
            jobs.append(("aur", entry))

        # Local and AUR entries are submitted together so network-bound AUR
        # checks and disk/CPU-bound local builds overlap instead of running
        # as two separate sequential batches.
        self._run_jobs(jobs)

        if self.check_only:
            heading("Check summary")
            print(f"Needs update/build: {count(self.stats.outdated, YELLOW)}")
            print(f"Unchecked:          {self.stats.unchecked}")
            print(f"Failed:             {count(self.stats.failed, RED)}")
            print(colored(SEPARATOR, DIM))
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
                    f"{RED}ERROR: Installing local packages failed{RESET}", file=sys.stderr
                )
                return 1

        if self.force_install_packages:
            heading("==> Reinstalling rebuilt packages")
            result = command(
                ["sudo", "pacman", "-U", "--", *self.force_install_packages]
            )
            if result.returncode != 0:
                print(
                    f"{RED}ERROR: Reinstalling rebuilt packages failed{RESET}",
                    file=sys.stderr,
                )
                return 1

        if not self.install_packages and not self.force_install_packages:
            print(f"\n{DIM}==> Nothing needs installing.{RESET}")

        print()
        print(colored(SEPARATOR, DIM))
        print(f"Processed: {self.stats.processed}")
        print(f"Failed:    {count(self.stats.failed, RED)}")
        print(colored(SEPARATOR, DIM))
        return 1 if self.stats.failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--check",
        action="store_true",
        help="check for updates without building or installing packages",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help=f"number of packages to check/build concurrently (default: {DEFAULT_JOBS})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return Installer(check_only=args.check, jobs=max(1, args.jobs)).run()


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
