#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from report_storage import (
    MAX_BROWSE_REPORTS,
    client_report_dir_candidates,
    is_allowed_report_name,
    list_report_files,
)


def test_max_browse_reports_is_30():
    assert MAX_BROWSE_REPORTS == 30


def test_client_report_dir_candidates():
    paths = client_report_dir_candidates("svinayak01")
    assert paths
    assert any("crm_compare_reports" in str(p).lower() for p in paths)


def test_is_allowed_new_report_names():
    assert is_allowed_report_name("crm_full_comparison_svinayak01_20260604_first_report.html")
    assert is_allowed_report_name("crm_full_comparison_svinayak01_20260604_second_report.txt")
    assert not is_allowed_report_name("../secret.txt")
    assert not is_allowed_report_name("random.txt")


def test_list_report_files_dedupes_txt_and_html():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        stem = "crm_full_comparison_user_20260604_first_report"
        txt = d / f"{stem}.txt"
        html = d / f"{stem}.html"
        txt.write_text("text", encoding="utf-8")
        html.write_text("<html></html>", encoding="utf-8")
        listed = list_report_files([d])
        assert len(listed) == 1
        assert listed[0].suffix.lower() == ".txt"


if __name__ == "__main__":
    test_max_browse_reports_is_30()
    test_client_report_dir_candidates()
    test_is_allowed_new_report_names()
    test_list_report_files_dedupes_txt_and_html()
    print("OK: report storage tests passed")
