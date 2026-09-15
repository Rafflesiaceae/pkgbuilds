#!/usr/bin/env python3

import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
