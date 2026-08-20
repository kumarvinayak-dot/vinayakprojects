#!/usr/bin/env python3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from report_naming import (
    allocate_report_filename,
    build_report_filename,
    comparison_path_slug,
    next_report_sequence,
    ordinal_report_slug,
    resolve_client_identity,
)


def test_comparison_path_slug():
    f1 = r"\\server\share\SDK_CRM_10.24.1.0_linux64_20260604_0933.jar"
    f2 = r"\\server\share\SDK_CRM_10.25.1.0_linux64_20260605_1200.jar"
    slug = comparison_path_slug(f1, f2)
    assert "vs" in slug
    assert "sdk_crm" in slug.lower()


def test_ordinal_labels():
    assert ordinal_report_slug(1) == "first_report"
    assert ordinal_report_slug(2) == "second_report"
    assert ordinal_report_slug(11) == "eleventh_report"
    assert ordinal_report_slug(21) == "report_21"


def test_allocate_report_filename_sequence():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        n1 = allocate_report_filename("crm_full_comparison", "svinayak01", d)
        assert "svinayak01" in n1
        assert "first_report" in n1
        assert n1.endswith(".txt")
        (d / n1).write_text("one", encoding="utf-8")
        n2 = allocate_report_filename("crm_full_comparison", "svinayak01", d)
        assert "second_report" in n2
        from report_naming import report_run_date

        assert next_report_sequence(d, "crm_full_comparison", "svinayak01", report_run_date()) == 2


def test_allocate_uses_save_dir_not_cwd():
    """Ordinal must count files in the report save folder, not the process cwd."""
    with tempfile.TemporaryDirectory() as save_tmp, tempfile.TemporaryDirectory() as cwd_tmp:
        save_dir = Path(save_tmp)
        cwd_dir = Path(cwd_tmp)
        first = allocate_report_filename("crm_full_comparison", "svinayak01", save_dir)
        (save_dir / first).write_text("saved", encoding="utf-8")
        wrong = allocate_report_filename("crm_full_comparison", "svinayak01", cwd_dir)
        right = allocate_report_filename("crm_full_comparison", "svinayak01", save_dir)
        assert "first_report" in wrong
        assert "second_report" in right


def test_build_report_filename_no_timezone_suffix():
    with tempfile.TemporaryDirectory() as tmp:
        name = build_report_filename(
            "crm_full_comparison",
            r"\\a\old.jar",
            r"\\b\new.jar",
            "svinayak01",
            report_dir=Path(tmp),
        )
        assert name.startswith("crm_full_comparison_svinayak01_")
        assert "_report." in name
        assert "indian" not in name.lower()
        assert "kolkata" not in name.lower()


def test_resolve_localhost_identity():
    ident = resolve_client_identity("127.0.0.1", {}, "")
    assert ident and ident != ""


if __name__ == "__main__":
    test_comparison_path_slug()
    test_ordinal_labels()
    test_allocate_report_filename_sequence()
    test_allocate_uses_save_dir_not_cwd()
    test_build_report_filename_no_timezone_suffix()
    test_resolve_localhost_identity()
    print("OK: report naming tests passed")
