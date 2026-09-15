#!/usr/bin/env python3

import io
import os
import shutil
import subprocess
import sys
import unittest
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
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys, "stdout", output),
        ):
            result = task.run_exclusive(["makepkg"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(command.call_args_list[0].args[0], ["sudo", "-n", "-v"])
        self.assertEqual(command.call_args_list[1].args[0], ["sudo", "-v"])
        self.assertIn("\033[?25h", output.getvalue())
        self.assertIn("\033[?25l", output.getvalue())


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


if __name__ == "__main__":
    unittest.main()
