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


class TtyOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


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


class CommandStreamingTest(unittest.TestCase):
    def test_lines_arrive_before_exit_and_keep_streams_separate(self) -> None:
        script = (
            "import os, sys, time\n"
            "print('ready')\n"
            "deadline = time.monotonic() + 2\n"
            "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "if not os.path.exists(sys.argv[1]):\n"
            "    sys.exit(3)\n"
            "print('result', flush=True)\n"
            "print('warning', file=sys.stderr, flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as root:
            signal = Path(root) / "continue"
            lines = []

            def on_output(line):
                lines.append(line)
                if line == "ready":
                    signal.touch()

            result = install.command(
                [sys.executable, "-c", script, signal],
                capture=True,
                parsed_output=True,
                on_output=on_output,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ready\nresult\n")
        self.assertEqual(result.stderr, "warning\n")
        self.assertEqual(lines[0], "ready")
        self.assertCountEqual(lines, ["ready", "result", "warning"])


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


class InstallerProgressTest(unittest.TestCase):
    def test_explicit_single_target_streams_without_spinner(self) -> None:
        installer = install.Installer(check_only=True, jobs=1, target="example")

        def finish_job(kind, entry, board, check_only, force):
            task = install.Task(board, f"{kind.upper()}: {entry}")
            task.log("Checking package...")
            task.run([sys.executable, "-c", "print('subprocess line', flush=True)"])
            task.finish()
            return task

        for output in (TtyOutput(), io.StringIO()):
            with self.subTest(isatty=output.isatty()):
                with (
                    patch.object(sys, "stdout", output),
                    patch.object(install, "run_task", side_effect=finish_job),
                ):
                    installer._run_jobs([("local", "example")])

                text = output.getvalue()
                self.assertIn("Started: LOCAL: example", text)
                self.assertLess(text.index("Checking package..."), text.index("subprocess line"))
                self.assertEqual(text.splitlines().count("subprocess line"), 1)
                self.assertIn("✔ LOCAL: example", text)
                self.assertNotIn("\033[?25l", text)
                self.assertNotIn(" running:", text)

    def test_explicit_single_target_still_shows_failures(self) -> None:
        installer = install.Installer(check_only=True, jobs=1, target="example")
        output = TtyOutput()

        def fail_job(kind, entry, board, check_only, force):
            task = install.Task(board, f"{kind.upper()}: {entry}")
            task.log("unique diagnostic")
            task.fail("build failed")
            task.finish()
            return task

        with (
            patch.object(sys, "stdout", output),
            patch.object(install, "run_task", side_effect=fail_job),
        ):
            installer._run_jobs([("local", "example")])

        self.assertIn("build failed", output.getvalue())
        self.assertEqual(output.getvalue().count("unique diagnostic"), 1)
        self.assertNotIn("\033[?25l", output.getvalue())
        self.assertEqual(installer.stats.failed, 1)

    def test_implicit_or_multiple_targets_keep_progress(self) -> None:
        for target, jobs in (
            (None, [("local", "example")]),
            (["example", "other"], [("local", "example"), ("local", "other")]),
        ):
            with self.subTest(target=target):
                installer = install.Installer(check_only=True, jobs=2, target=target)
                output = TtyOutput()

                def finish_job(kind, entry, board, check_only, force):
                    task = install.Task(board, f"{kind.upper()}: {entry}")
                    task.finish()
                    return task

                with (
                    patch.object(sys, "stdout", output),
                    patch.object(install, "run_task", side_effect=finish_job),
                ):
                    installer._run_jobs(jobs)

                self.assertIn("\033[?25l", output.getvalue())
                self.assertIn("LOCAL: example", output.getvalue())


class InstallerTargetTest(unittest.TestCase):
    def test_arch_target_selects_unlisted_local_checkout(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="chromium")
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "chromium"
            directory.mkdir()
            (directory / "PKGBUILD").touch()

            with (
                patch.object(install, "PKGBUILD_ROOT", Path(root)),
                patch.object(install, "read_package_list", return_value=["other"]),
                patch.object(installer, "_run_jobs") as run_jobs,
                patch.object(sys, "stdout", output),
            ):
                result = installer._run_arch()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with([("local", "chromium")])

    def test_unlisted_local_target_works_without_install_lists(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="chromium")
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as root:
            package_root = Path(root)
            directory = package_root / "chromium"
            directory.mkdir()
            (directory / "PKGBUILD").touch()

            with (
                patch.object(install, "PKGBUILD_ROOT", package_root),
                patch.object(install, "INSTALL_LIST", package_root / "install-list"),
                patch.object(install, "INSTALL_LIST_AUR", package_root / "install-list-aur"),
                patch.object(install, "is_ubuntu_24", return_value=False),
                patch.object(installer, "_run_jobs") as run_jobs,
                patch.object(sys, "stdout", output),
            ):
                result = installer.run()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with([("local", "chromium")])

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

    def test_ubuntu_target_selects_unlisted_local_checkout(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="local-script")
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "local-script"
            directory.mkdir()
            (directory / "ubuntu24.py").touch()

            with (
                patch.object(install, "PKGBUILD_ROOT", Path(root)),
                patch.object(install, "read_package_list", return_value=["other"]),
                patch.object(installer, "_run_jobs") as run_jobs,
                patch.object(sys, "stdout", output),
            ):
                result = installer._run_ubuntu24()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with([("ubuntu24", "local-script")])

    def test_ubuntu_force_selects_multiple_targets(self) -> None:
        installer = install.Installer(
            check_only=False, jobs=2, target=["first", "third"], force=True
        )
        with (
            patch.object(
                install, "read_package_list", return_value=["first", "second", "third"]
            ),
            patch.object(installer, "_run_jobs") as run_jobs,
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = installer._run_ubuntu24()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with(
            [("ubuntu24", "first"), ("ubuntu24", "third")]
        )

    def test_unlisted_aur_checkout_is_not_selected(self) -> None:
        installer = install.Installer(check_only=True, jobs=2, target="aur-only")
        error = io.StringIO()

        with tempfile.TemporaryDirectory() as root:
            package_root = Path(root)
            directory = package_root / ".aur-build" / "aur-only"
            directory.mkdir(parents=True)
            (directory / "PKGBUILD").touch()

            with (
                patch.object(install, "PKGBUILD_ROOT", package_root),
                patch.object(install, "read_package_list", return_value=["other"]),
                patch.object(installer, "_run_jobs") as run_jobs,
                patch.object(sys, "stderr", error),
            ):
                result = installer._run_arch()

        self.assertEqual(result, 1)
        run_jobs.assert_not_called()
        self.assertIn("Target 'aur-only' was not found", error.getvalue())

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
        self.assertEqual(args.target, ["rustdesk"])

    def test_force_selects_only_requested_targets(self) -> None:
        installer = install.Installer(
            check_only=False, jobs=2, target=["local-one", "aur-one"], force=True
        )

        def package_entries(path):
            if path == install.INSTALL_LIST:
                return ["local-one", "local-two"]
            return ["aur-one", "aur-two"]

        with (
            patch.object(install, "read_package_list", side_effect=package_entries),
            patch.object(installer, "_run_jobs") as run_jobs,
            patch.object(install, "AUR_BUILD_ROOT", Path(tempfile.gettempdir())),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = installer._run_arch()

        self.assertEqual(result, 0)
        self.assertTrue(installer.force)
        run_jobs.assert_called_once_with(
            [("local", "local-one"), ("aur", "aur-one")]
        )

    def test_missing_target_prevents_partial_force_run(self) -> None:
        installer = install.Installer(
            check_only=False, jobs=2, target=["known", "missing"], force=True
        )
        with (
            patch.object(install, "read_package_list", return_value=["known"]),
            patch.object(installer, "_run_jobs") as run_jobs,
            patch.object(sys, "stderr", io.StringIO()) as error,
        ):
            result = installer._run_arch()

        self.assertEqual(result, 1)
        run_jobs.assert_not_called()
        self.assertIn("Target 'missing' was not found", error.getvalue())

    def test_force_flag_is_passed_to_installer(self) -> None:
        with (
            patch.object(sys, "argv", ["install.py", "-f", "local-one", "aur-one"]),
            patch.object(install, "Installer") as installer,
        ):
            installer.return_value.run.return_value = 0
            result = install.main()

        self.assertEqual(result, 0)
        self.assertTrue(installer.call_args.kwargs["force"])
        self.assertEqual(
            installer.call_args.kwargs["target"], ["local-one", "aur-one"]
        )
        installer.return_value.run.assert_called_once_with()

    def test_force_option_can_appear_between_targets(self) -> None:
        with patch.object(
            sys, "argv", ["install.py", "local-one", "--force", "aur-one"]
        ):
            args = install.parse_args()

        self.assertTrue(args.force)
        self.assertEqual(args.target, ["local-one", "aur-one"])

    def test_force_cannot_be_combined_with_check(self) -> None:
        with (
            patch.object(sys, "argv", ["install.py", "--force", "--check"]),
            patch.object(sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            install.parse_args()

        self.assertEqual(raised.exception.code, 2)


class ForceInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.task = install.Task(
            install.StatusBoard(enabled=False), "LOCAL: example"
        )

    def test_local_force_rebuilds_cached_archive_and_queues_reinstall(self) -> None:
        directory = self.root / "example"
        directory.mkdir()
        (directory / "PKGBUILD").touch()
        package = directory / "example-1-1-x86_64.pkg.tar.zst"
        package.touch()

        with (
            patch.object(install, "PKGBUILD_ROOT", self.root),
            patch.object(install, "makepkg_package_list", return_value=[package]),
            patch.object(install, "find_built_package", return_value=package),
            patch.object(
                self.task,
                "run_exclusive",
                return_value=subprocess.CompletedProcess([], 0),
            ) as build,
        ):
            install.process_local("example", self.task, check_only=False, force=True)

        self.assertTrue(self.task.ok)
        self.assertEqual(self.task.queued_force, package)
        build.assert_called_once_with(
            ["makepkg", "-fsc", "--noconfirm"],
            cwd=directory,
            extra_env={"PKGDEST": str(directory)},
        )

    def test_aur_force_bypasses_newer_installed_version_and_cached_build(self) -> None:
        aur_root = self.root / ".aur-build"
        directory = aur_root / "example"
        (directory / ".git").mkdir(parents=True)
        (directory / "PKGBUILD").touch()
        package = directory / "example-1-1-x86_64.pkg.tar.zst"

        with (
            patch.object(install, "AUR_BUILD_ROOT", aur_root),
            patch.object(
                self.task,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, "Version: 1.0\nPackage Base: example\n", ""
                ),
            ),
            patch.object(install, "installed_version", return_value="2.0"),
            patch.object(install, "compare_versions", return_value=-1),
            patch.object(install, "makepkg_dependencies", return_value=set()),
            patch.object(install, "find_cached_aur_package") as cached,
            patch.object(install, "makepkg_package_list", return_value=[package]),
            patch.object(install, "find_built_package", return_value=package),
            patch.object(install, "package_info", return_value=("example", "1.0")),
            patch.object(
                self.task,
                "run_exclusive",
                return_value=subprocess.CompletedProcess([], 0),
            ) as build,
        ):
            install.process_aur("example", self.task, check_only=False, force=True)

        self.assertTrue(self.task.ok)
        self.assertEqual(self.task.queued_force, package)
        cached.assert_not_called()
        build.assert_called_once_with(
            ["makepkg", "-fsc", "--noconfirm"],
            cwd=directory,
            extra_env={"PKGDEST": str(directory)},
        )

    def test_ubuntu_force_passes_flag_to_package_script(self) -> None:
        directory = self.root / "example"
        directory.mkdir()
        script = directory / "ubuntu24.py"
        script.touch()

        with (
            patch.object(install, "PKGBUILD_ROOT", self.root),
            patch.object(
                self.task,
                "run_exclusive",
                return_value=subprocess.CompletedProcess([], 0),
            ) as build,
        ):
            install.process_ubuntu24(
                "example", self.task, check_only=False, force=True
            )

        self.assertEqual(self.task.rebuilt, 1)
        build.assert_called_once_with(
            ["python", script, "--install", "--force"], cwd=directory
        )

    def test_force_reinstall_omits_pacman_needed(self) -> None:
        installer = install.Installer(check_only=False, jobs=1, force=True)
        package = self.root / "example-1-1-x86_64.pkg.tar.zst"

        def package_entries(path):
            return ["example", "other"] if path == install.INSTALL_LIST else []

        def queue_package(_jobs):
            installer.force_install_packages.append(package)

        with (
            patch.object(install, "read_package_list", side_effect=package_entries),
            patch.object(installer, "_run_jobs", side_effect=queue_package) as run_jobs,
            patch.object(
                install,
                "command",
                return_value=subprocess.CompletedProcess([], 0),
            ) as command,
            patch.object(install, "notify_sudo_prompt"),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = installer._run_arch()

        self.assertEqual(result, 0)
        run_jobs.assert_called_once_with(
            [("local", "example"), ("local", "other")]
        )
        command.assert_called_once_with(["sudo", "pacman", "-U", "--", package])


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

    def test_clean_repairs_owner_permissions_without_sudo(self) -> None:
        package = self.package(self.root, "restricted")
        workdir = package / "pkg"
        workdir.chmod(0o111)
        real_rmtree = shutil.rmtree

        def remove(path):
            if Path(path) == workdir and not workdir.stat().st_mode & 0o400:
                raise PermissionError("owner cannot read pkg")
            return real_rmtree(path)

        with (
            patch.object(install.shutil, "rmtree", side_effect=remove),
            patch.object(install, "command") as command,
            patch.object(sys, "stdout", io.StringIO()),
        ):
            result = install.clean_build_directories("restricted")

        self.assertEqual(result, 0)
        self.assertFalse(workdir.exists())
        self.assertTrue((package / "restricted-1-1-x86_64.pkg.tar.zst").exists())
        command.assert_not_called()

    def test_clean_uses_sudo_only_for_a_permission_failure(self) -> None:
        package = self.package(self.root, "root-owned")
        shutil.rmtree(package / "src")
        workdir = package / "pkg"
        command_result = subprocess.CompletedProcess([], 1)

        with (
            patch.object(install.shutil, "rmtree", side_effect=PermissionError("denied")),
            patch.object(install, "command", return_value=command_result) as command,
            patch.object(install, "notify_sudo_prompt") as notify,
            patch.object(sys, "stderr", io.StringIO()) as error,
        ):
            result = install.clean_build_directories("root-owned")

        self.assertEqual(result, 1)
        command.assert_called_once_with(["sudo", "rm", "-rf", "--", workdir])
        notify.assert_called_once_with(f"Cleaning {workdir}")
        self.assertIn("sudo rm failed with exit code 1", error.getvalue())
        self.assertTrue(workdir.exists())


if __name__ == "__main__":
    unittest.main()
