#!/usr/bin/env python3
"""Tests for WEF remote SSH comparison."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wef_compare import WEFComparer, WEFComparisonResult, run_wef_comparison  # noqa: E402


class _FakeSshSession:
    def __init__(self, listings: dict[str, str]) -> None:
        self._listings = listings

    def exec_command(self, cmd: str, timeout: int = 3600):
        for root, body in self._listings.items():
            if root in cmd:
                return 0, body, ""
        return 2, "__WEF_ERR__:not_a_directory\n", ""

    def close(self) -> None:
        pass


class WEFCompareTests(unittest.TestCase):
    def test_compare_detects_added_removed_modified(self) -> None:
        old_listing = "100\tlib/a.jar\n200\tlib/b.jar\n"
        new_listing = "100\tlib/a.jar\n300\tlib/b.jar\n150\tlib/c.jar\n"
        session = _FakeSshSession(
            {
                "/build/old": old_listing,
                "/build/new": new_listing,
            }
        )

        comparer = WEFComparer(
            ssh_host="unix.example",
            ssh_username="user",
            ssh_password="secret",
            old_path="/build/old",
            new_path="/build/new",
        )

        with patch("wef_compare._connect_ssh", return_value=session):
            results = comparer.compare()

        self.assertIsNone(comparer.comparison_error)
        self.assertEqual(comparer.total_files_old, 2)
        self.assertEqual(comparer.total_files_new, 3)
        self.assertEqual(comparer.count_unchanged, 1)
        statuses = {r.status: r for r in results}
        self.assertNotIn("unchanged", statuses)
        self.assertIn("lib/b.jar", statuses["modified"].relative_path)
        self.assertEqual(statuses["modified"].size_old, 200)
        self.assertEqual(statuses["modified"].size_new, 300)
        self.assertEqual(statuses["added"].relative_path, "lib/c.jar")

    def test_large_unchanged_set_does_not_store_every_row(self) -> None:
        old_lines = [f"{i}\tfile{i}.dat" for i in range(5000)]
        new_lines = [f"{i}\tfile{i}.dat" for i in range(5000)]
        new_lines.append("99\textra.jar\n")
        session = _FakeSshSession(
            {
                "/old": "\n".join(old_lines) + "\n",
                "/new": "\n".join(new_lines),
            }
        )
        comparer = WEFComparer(
            ssh_host="h",
            ssh_username="u",
            ssh_password="p",
            old_path="/old",
            new_path="/new",
        )
        with patch("wef_compare._connect_ssh", return_value=session):
            results = comparer.compare()
        self.assertEqual(comparer.count_unchanged, 5000)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "added")

    def test_generate_html_report_contains_summary(self) -> None:
        comparer = WEFComparer(
            ssh_host="h",
            ssh_username="u",
            ssh_password="p",
            old_path="/old",
            new_path="/new",
        )
        comparer.total_files_old = 1
        comparer.total_files_new = 2
        comparer.results = [
            WEFComparisonResult(
                relative_path="x.txt",
                status="added",
                size_new=10,
                details="Only in new build",
            )
        ]
        html = comparer.generate_html_report()
        self.assertIn("WEF Remote Comparison", html)
        self.assertIn("x.txt", html)

    def test_run_wef_comparison_saves_reports(self) -> None:
        old_listing = "10\ta.txt\n"
        new_listing = "10\ta.txt\n20\tb.txt\n"
        session = _FakeSshSession({"/o": old_listing, "/n": new_listing})

        with tempfile.TemporaryDirectory() as tmp:
            with patch("wef_compare._connect_ssh", return_value=session):
                comparer, results, name = run_wef_comparison(
                    "host",
                    "user",
                    "pass",
                    "/o",
                    "/n",
                    report_dir=tmp,
                    report_user="tester",
                )
                comparer.save_report(str(Path(tmp) / name))
            self.assertTrue(name.endswith(".txt"))
            txt_path = Path(tmp) / name
            html_path = txt_path.with_suffix(".html")
            self.assertTrue(txt_path.is_file())
            self.assertTrue(html_path.is_file())
            self.assertGreater(len(results), 0)
            self.assertIsNone(comparer.comparison_error)


if __name__ == "__main__":
    unittest.main()
