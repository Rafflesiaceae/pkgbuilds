#!/usr/bin/env python3

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import install


class StatusBoardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.board = install.StatusBoard(enabled=True)
        self.board._order = ["task"]
        self.board._tasks["task"] = SimpleNamespace(
            status_label="running: " + "dependency " * 20,
        )

    def test_status_text_stays_shorter_than_terminal_width(self) -> None:
        with patch.object(
            shutil, "get_terminal_size", return_value=os.terminal_size((40, 24))
        ):
            text = self.board._status_text()

        plain_text = install.ANSI_ESCAPE.sub("", text)
        self.assertLess(install.terminal_text_width(plain_text), 40)
        self.assertTrue(plain_text.endswith("…"))

    def test_status_text_uses_current_terminal_width(self) -> None:
        # Querying on every render makes resizing safe while a task is active.
        with patch.object(
            shutil,
            "get_terminal_size",
            side_effect=[
                os.terminal_size((120, 24)),
                os.terminal_size((30, 24)),
            ],
        ):
            wide = install.ANSI_ESCAPE.sub("", self.board._status_text())
            narrow = install.ANSI_ESCAPE.sub("", self.board._status_text())

        self.assertGreater(install.terminal_text_width(wide), 29)
        self.assertLess(install.terminal_text_width(narrow), 30)

    def test_status_text_removes_embedded_color_sequences(self) -> None:
        self.board._tasks["task"].status_label = "\033[2m$\033[0m long command"

        with patch.object(
            shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
        ):
            text = self.board._status_text()

        # The renderer adds only the frame's cyan and reset sequences.
        self.assertEqual(text.count("\033["), 2 if install.COLOR else 0)
        self.assertIn("$ long command", install.ANSI_ESCAPE.sub("", text))

    def test_suspend_buffers_finished_tasks_until_prompt_is_done(self) -> None:
        self.board._line_drawn = True
        output = io.StringIO()

        with patch.object(sys, "stdout", output):
            self.board.suspend()
            self.board.finish_task("task", "finished", None)
            suspended_output = output.getvalue()
            self.board.resume()

        self.assertNotIn("finished", suspended_output)
        self.assertIn("finished", output.getvalue())


class InteractiveSudoTest(unittest.TestCase):
    def test_missing_sudo_timestamp_is_authenticated_outside_spinner(self) -> None:
        board = install.StatusBoard(enabled=True)
        task = install.Task(board, "LOCAL: example")
        command_results = [
            subprocess.CompletedProcess(["sudo", "-n", "-v"], 1, "", ""),
            subprocess.CompletedProcess(["sudo", "-v"], 0, None, None),
            subprocess.CompletedProcess(["makepkg"], 0, "", ""),
        ]
        output = io.StringIO()

        with (
            patch.object(install, "command", side_effect=command_results) as command,
            patch.object(install, "notify_sudo_prompt") as notify,
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys, "stdout", output),
        ):
            result = task.run_exclusive(["makepkg"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(command.call_args_list[0].args[0], ["sudo", "-n", "-v"])
        self.assertEqual(command.call_args_list[1].args[0], ["sudo", "-v"])
        notify.assert_called_once_with("LOCAL: example")
        self.assertIn("\033[?25h", output.getvalue())
        self.assertIn("\033[?25l", output.getvalue())

    def test_valid_sudo_timestamp_routes_nested_sudo_through_wrapper(self) -> None:
        board = install.StatusBoard(enabled=False)
        command_results = [
            subprocess.CompletedProcess(["sudo", "-n", "-v"], 0, "", ""),
            subprocess.CompletedProcess(["makepkg"], 0, "", ""),
        ]
        output = io.StringIO()

        with (
            patch.object(
                install,
                "command",
                side_effect=command_results,
            ) as command,
            patch.object(install, "notify_sudo_prompt") as notify,
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys, "stdout", output),
        ):
            task = install.Task(board, "LOCAL: example")
            result = task.run_exclusive(["makepkg"])

        self.assertEqual(result.returncode, 0)
        notify.assert_not_called()
        command_env = command.call_args_list[-1].kwargs["extra_env"]
        self.assertEqual(command_env["INSTALL_PY_SUDO_CONTEXT"], "LOCAL: example")
        self.assertEqual(
            command_env["PATH"].split(os.pathsep)[0],
            str(install.SUDO_WRAPPER.parent),
        )

    def test_nested_sudo_environment_prepends_wrapper(self) -> None:
        with patch.object(install.shutil, "which", return_value="/usr/bin/sudo"):
            env = install.nested_sudo_environment(
                "LOCAL: example",
                {"PATH": "/usr/bin", "PKGDEST": "/packages"},
            )

        self.assertEqual(
            env["PATH"].split(os.pathsep),
            [str(install.SUDO_WRAPPER.parent), "/usr/bin"],
        )
        self.assertEqual(env["INSTALL_PY_SUDO_CONTEXT"], "LOCAL: example")
        self.assertEqual(env["INSTALL_PY_REAL_SUDO"], "/usr/bin/sudo")
        self.assertEqual(env["PKGDEST"], "/packages")

    def test_nested_sudo_wrapper_notifies_at_actual_invocation(self) -> None:
        wrapper_env = {
            "INSTALL_PY_STARTED_AT": "10.0",
            "INSTALL_PY_SUDO_CONTEXT": "LOCAL: example",
            "INSTALL_PY_REAL_SUDO": "/usr/bin/sudo",
        }

        with (
            patch.dict(os.environ, wrapper_env, clear=True),
            patch.object(install.time, "monotonic", return_value=12.0),
            patch.object(install, "_send_sudo_notification") as send,
            patch.object(
                install.os,
                "execv",
                side_effect=RuntimeError("exec"),
            ) as execv,
            self.assertRaisesRegex(RuntimeError, "exec"),
        ):
            install.run_sudo_wrapper(["-k", "true"])

        send.assert_called_once_with("LOCAL: example")
        execv.assert_called_once_with(
            "/usr/bin/sudo",
            ["/usr/bin/sudo", "-k", "true"],
        )

    def test_nested_sudo_wrapper_swallows_startup_notification(self) -> None:
        wrapper_env = {
            "INSTALL_PY_STARTED_AT": "10.0",
            "INSTALL_PY_REAL_SUDO": "/usr/bin/sudo",
        }

        with (
            patch.dict(os.environ, wrapper_env, clear=True),
            patch.object(install.time, "monotonic", return_value=11.5),
            patch.object(install, "_send_sudo_notification") as send,
            patch.object(
                install.os,
                "execv",
                side_effect=RuntimeError("exec"),
            ),
            self.assertRaisesRegex(RuntimeError, "exec"),
        ):
            install.run_sudo_wrapper(["true"])

        send.assert_not_called()

    def test_sudo_notification_uses_notify_send(self) -> None:
        with (
            patch.object(install, "PROCESS_STARTED_AT", 0.0),
            patch.object(install.time, "monotonic", return_value=2.0),
            patch.object(install.subprocess, "run") as run,
        ):
            install.notify_sudo_prompt("LOCAL: example")

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[0], "notify-send")
        self.assertIn("--urgency=critical", arguments)
        self.assertIn("LOCAL: example", arguments[-1])

    def test_sudo_notification_failure_does_not_break_install(self) -> None:
        with (
            patch.object(install, "PROCESS_STARTED_AT", 0.0),
            patch.object(install.time, "monotonic", return_value=2.0),
            patch.object(
                install.subprocess,
                "run",
                side_effect=FileNotFoundError("notify-send"),
            ),
        ):
            install.notify_sudo_prompt("LOCAL: example")

    def test_sudo_notification_is_suppressed_during_startup(self) -> None:
        with (
            patch.object(install, "PROCESS_STARTED_AT", 10.0),
            patch.object(install.time, "monotonic", return_value=10.5),
            patch.object(install, "_send_sudo_notification") as send,
        ):
            install.notify_sudo_prompt("LOCAL: example")

        send.assert_not_called()


class InstallerSummaryTest(unittest.TestCase):
    def test_failed_entries_are_listed_in_stable_order(self) -> None:
        installer = install.Installer(check_only=True, jobs=2)
        installer.failed_entries = ["LOCAL: zeta", "AUR: alpha"]
        output = io.StringIO()

        with patch.object(sys, "stdout", output):
            installer._print_failed_entries()

        self.assertEqual(
            output.getvalue(),
            "Failed entries:\n  AUR: alpha\n  LOCAL: zeta\n",
        )


class InstallerTargetTest(unittest.TestCase):
    def test_arch_target_selects_only_matching_local_entry(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="rustdesk")
        output = io.StringIO()

        def package_entries(path):
            if path == install.INSTALL_LIST:
                return ["other-local", "rustdesk"]
            return ["other-aur"]

        with (
            patch.object(install, "read_package_list", side_effect=package_entries),
            patch.object(installer, "_run_jobs") as run_jobs,
            patch.object(sys, "stdout", output),
        ):
            result = installer._run_arch()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with([("local", "rustdesk")])

    def test_arch_target_selects_only_matching_aur_entry(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="aur-target")
        output = io.StringIO()

        def package_entries(path):
            if path == install.INSTALL_LIST:
                return ["other-local"]
            return ["other-aur", "aur-target"]

        with (
            patch.object(install, "read_package_list", side_effect=package_entries),
            patch.object(installer, "_run_jobs") as run_jobs,
            patch.object(sys, "stdout", output),
        ):
            result = installer._run_arch()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with([("aur", "aur-target")])

    def test_ubuntu_target_selects_only_matching_entry(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="rustdesk")
        output = io.StringIO()

        with (
            patch.object(
                install,
                "read_package_list",
                return_value=["other-local", "rustdesk"],
            ),
            patch.object(installer, "_run_jobs") as run_jobs,
            patch.object(sys, "stdout", output),
        ):
            result = installer._run_ubuntu24()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with([("ubuntu24", "rustdesk")])

    def test_unknown_target_fails_without_running_jobs(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="missing")
        error = io.StringIO()

        with (
            patch.object(install, "read_package_list", return_value=["other"]),
            patch.object(installer, "_run_jobs") as run_jobs,
            patch.object(sys, "stderr", error),
        ):
            result = installer._run_arch()

        self.assertEqual(result, 1)
        run_jobs.assert_not_called()
        self.assertIn("Target 'missing' was not found", error.getvalue())

    def test_positional_target_is_parsed_with_options(self) -> None:
        with patch.object(sys, "argv", ["install.py", "--check", "rustdesk"]):
            args = install.parse_args()

        self.assertTrue(args.check)
        self.assertEqual(args.target, "rustdesk")


class ListTargetsTest(unittest.TestCase):
    def test_short_list_option_prints_local_and_aur_targets_once(self) -> None:
        output = io.StringIO()

        def package_entries(path):
            if path == install.INSTALL_LIST:
                return ["local-one", "shared"]
            return ["aur-one", "shared"]

        with (
            patch.object(sys, "argv", ["install.py", "-l"]),
            patch.object(install, "read_package_list", side_effect=package_entries),
            patch.object(install.Installer, "run") as installer_run,
            patch.object(sys, "stdout", output),
        ):
            result = install.main()

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "local-one\nshared\naur-one\n")
        installer_run.assert_not_called()

    def test_list_fails_when_both_configuration_files_are_missing(self) -> None:
        error = io.StringIO()

        with (
            patch.object(install.Path, "is_file", return_value=False),
            patch.object(sys, "stderr", error),
        ):
            result = install.list_targets()

        self.assertEqual(result, 1)
        self.assertIn(
            "Neither install-list nor install-list-aur exists",
            error.getvalue(),
        )

    def test_list_cannot_be_combined_with_a_target(self) -> None:
        error = io.StringIO()

        with (
            patch.object(sys, "argv", ["install.py", "--list", "rustdesk"]),
            patch.object(sys, "stderr", error),
            self.assertRaises(SystemExit) as raised,
        ):
            install.parse_args()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--list cannot be combined with a target", error.getvalue())


class CleanBuildDirectoriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.aur_root = self.root / ".aur-build"
        root_patch = patch.object(install, "PKGBUILD_ROOT", self.root)
        aur_patch = patch.object(install, "AUR_BUILD_ROOT", self.aur_root)
        root_patch.start()
        aur_patch.start()
        self.addCleanup(root_patch.stop)
        self.addCleanup(aur_patch.stop)

    def package(self, parent: Path, name: str) -> Path:
        directory = parent / name
        directory.mkdir(parents=True)
        (directory / "PKGBUILD").touch()
        for workdir in ("src", "pkg"):
            (directory / workdir).mkdir()
            (directory / workdir / "build-output").touch()
        (directory / f"{name}-1-1-x86_64.pkg.tar.zst").touch()
        return directory

    def test_clean_all_keeps_artifacts_and_unrelated_directories(self) -> None:
        local = self.package(self.root, "local")
        aur = self.package(self.aur_root, "aur")
        unrelated = self.root / "unrelated" / "src"
        unrelated.mkdir(parents=True)

        with patch.object(sys, "stdout", io.StringIO()):
            result = install.clean_build_directories(None)

        self.assertEqual(result, 0)
        for directory in (local, aur):
            self.assertFalse((directory / "src").exists())
            self.assertFalse((directory / "pkg").exists())
            self.assertTrue(
                (directory / f"{directory.name}-1-1-x86_64.pkg.tar.zst").exists()
            )
        self.assertTrue(unrelated.exists())

    def test_target_cleans_only_matching_package(self) -> None:
        selected = self.package(self.root, "selected")
        other = self.package(self.root, "other")

        with (
            patch.object(sys, "argv", ["install.py", "--clean", "selected"]),
            patch.object(install.Installer, "run") as installer_run,
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = install.main()

        self.assertEqual(result, 0)
        self.assertFalse((selected / "src").exists())
        self.assertFalse((selected / "pkg").exists())
        self.assertTrue((other / "src").exists())
        self.assertTrue((other / "pkg").exists())
        installer_run.assert_not_called()

    def test_aur_package_name_can_select_a_different_pkgbase(self) -> None:
        aur = self.package(self.aur_root, "shared-base")
        (aur / ".SRCINFO").write_text("pkgbase = shared-base\n\tpkgname = aur-name\n")

        with patch.object(sys, "stdout", io.StringIO()):
            result = install.clean_build_directories("aur-name")

        self.assertEqual(result, 0)
        self.assertFalse((aur / "src").exists())
        self.assertFalse((aur / "pkg").exists())

    def test_unknown_target_fails_without_removing_work(self) -> None:
        package = self.package(self.root, "known")

        with patch.object(sys, "stderr", io.StringIO()) as error:
            result = install.clean_build_directories("missing")

        self.assertEqual(result, 1)
        self.assertIn("Target 'missing' was not found", error.getvalue())
        self.assertTrue((package / "src").exists())

    def test_work_directory_symlink_does_not_remove_its_target(self) -> None:
        package = self.package(self.root, "linked")
        external = self.root / "external"
        external.mkdir()
        (external / "keep").touch()
        shutil.rmtree(package / "src")
        (package / "src").symlink_to(external, target_is_directory=True)

        with patch.object(sys, "stdout", io.StringIO()):
            result = install.clean_build_directories("linked")

        self.assertEqual(result, 0)
        self.assertFalse((package / "src").is_symlink())
        self.assertTrue((external / "keep").exists())


if __name__ == "__main__":
    unittest.main()
