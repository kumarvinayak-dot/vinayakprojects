#!/usr/bin/env python3
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "src"))

from client_temp_paths import (
    cleanup_compare_temp_before_run,
    client_temp_dir_unc_candidates,
    comparison_space_status,
    estimate_comparison_space_bytes,
    is_compare_temp_entry_for_application,
    is_compare_temp_entry_name,
    min_compare_space_bytes,
    normalize_application_slug,
    resolve_client_machine_hostname,
    set_compare_client_context,
    clear_compare_client_context,
)
from folder_compare import get_compare_temp_dir_candidates, _compare_temp_disk_probe


def test_resolve_client_machine_from_override():
    host = resolve_client_machine_hostname("10.1.2.3", {}, "svinayak01.corp.local")
    assert host == "svinayak01"


def test_client_temp_unc_candidates():
    paths = client_temp_dir_unc_candidates("svinayak01")
    assert paths
    assert any("svinayak01" in str(p).lower() for p in paths)
    assert any("crm_compare_temp" in str(p).lower() for p in paths)


def test_client_temp_skipped_for_localhost():
    assert client_temp_dir_unc_candidates("localhost") == []


def test_estimate_space_floor():
    os.environ.pop("CRM_MIN_COMPARE_SPACE_GB", None)
    need = estimate_comparison_space_bytes("", "")
    assert need >= min_compare_space_bytes()


def test_estimate_space_skips_network_path_walk():
    unc = r"\\dvciisilon02\cm_share2\v10.24.1\v10.24.1_67_release"
    with patch("client_temp_paths.os.walk") as mock_walk:
        need = estimate_comparison_space_bytes(unc, unc)
        mock_walk.assert_not_called()
    assert need >= min_compare_space_bytes()


def test_comparison_space_status_shape():
    st = comparison_space_status("svinayak01", "", "")
    assert "minimum_required_gb" in st
    assert st["minimum_required_gb"] >= 1
    assert "client_machine" in st
    assert st["client_machine"] == "svinayak01"
    assert "temp_strategy" in st


def test_disk_probe_unc_c_share():
    probe = _compare_temp_disk_probe(Path(r"\\svinayak01\c$\crm_compare_temp"))
    assert "svinayak01" in str(probe).lower()
    assert "c$" in str(probe).lower()


def test_is_compare_temp_entry_name():
    assert is_compare_temp_entry_name("folder_compare_abc123")
    assert is_compare_temp_entry_name("net_xyz")
    assert is_compare_temp_entry_name("crm_class_diff_foo")
    assert is_compare_temp_entry_name("zAb12CdE")
    assert not is_compare_temp_entry_name("WindowsUpdate")
    assert not is_compare_temp_entry_name("")


def test_normalize_application_slug():
    assert normalize_application_slug("CRM") == "crm"
    assert normalize_application_slug("UXF/OMNI") == "uxf"
    assert normalize_application_slug("ASCF") == "ascf"
    assert normalize_application_slug("DEX") == "dex"


def test_is_compare_temp_entry_for_application():
    assert is_compare_temp_entry_for_application("crm_class_diff_foo", "CRM")
    assert is_compare_temp_entry_for_application("folder_compare_abc", "ASCF")
    assert not is_compare_temp_entry_for_application("crm_class_diff_foo", "ASCF")
    assert not is_compare_temp_entry_for_application("WindowsUpdate", "CRM")


def test_cleanup_compare_temp_before_run_local(tmp_path):
    dedicated = tmp_path / "crm_compare_temp"
    dedicated.mkdir()
    old = dedicated / "folder_compare_oldrun"
    old.mkdir()
    (old / "build.jar").write_bytes(b"x" * 1024)
    general = tmp_path / "Temp"
    general.mkdir()
    keep = general / "WindowsUpdate.log"
    keep.write_text("keep")
    stale = general / "net_oldcopy"
    stale.mkdir()
    (stale / "data.bin").write_bytes(b"y" * 512)

    import client_temp_paths as ctp

    orig_dedicated = ctp.compare_temp_dedicated_roots
    orig_general = ctp.compare_temp_general_dirs
    try:
        ctp.compare_temp_dedicated_roots = lambda _host="": [dedicated]
        ctp.compare_temp_general_dirs = lambda _host="": [general]
        summary = cleanup_compare_temp_before_run("svinayak01")
    finally:
        ctp.compare_temp_dedicated_roots = orig_dedicated
        ctp.compare_temp_general_dirs = orig_general

    assert summary["removed"] == 2
    assert not old.exists()
    assert not stale.exists()
    assert keep.exists()
    assert summary["freed_bytes"] >= 1024 + 512


def test_cleanup_compare_temp_per_application(tmp_path):
    dedicated = tmp_path / "crm_compare_temp"
    dedicated.mkdir()
    crm_sub = dedicated / "crm"
    crm_sub.mkdir()
    crm_old = crm_sub / "net_crmrun"
    crm_old.mkdir()
    (crm_old / "data.bin").write_bytes(b"x" * 256)
    ascf_sub = dedicated / "ascf"
    ascf_sub.mkdir()
    ascf_old = ascf_sub / "folder_compare_ascfrun"
    ascf_old.mkdir()
    (ascf_old / "build.jar").write_bytes(b"y" * 512)
    general = tmp_path / "Temp"
    general.mkdir()
    crm_flat = general / "crm_playbooks_old"
    crm_flat.mkdir()
    ascf_flat = general / "ascf_oldcopy"
    ascf_flat.mkdir()

    import client_temp_paths as ctp

    orig_dedicated = ctp.compare_temp_dedicated_roots
    orig_general = ctp.compare_temp_general_dirs
    try:
        ctp.compare_temp_dedicated_roots = lambda _host="": [dedicated]
        ctp.compare_temp_general_dirs = lambda _host="": [general]
        summary = cleanup_compare_temp_before_run("svinayak01", application="CRM")
    finally:
        ctp.compare_temp_dedicated_roots = orig_dedicated
        ctp.compare_temp_general_dirs = orig_general

    assert summary["application_slug"] == "crm"
    assert not crm_old.exists()
    assert not crm_flat.exists()
    assert ascf_old.exists()
    assert ascf_flat.exists()
    assert summary["removed"] >= 2


def test_candidate_priority_client_before_remote():
    set_compare_client_context("svinayak01", "svinayak01", "CRM")
    try:
        fake_usage = {
            r"\\svinayak01\c$": type("DU", (), {"free": 100 * 1024**3, "total": 200 * 1024**3, "used": 100 * 1024**3})(),
            "C:\\": type("DU", (), {"free": 500 * 1024**3, "total": 600 * 1024**3, "used": 100 * 1024**3})(),
            r"\\inpnqbdnc04\software": type("DU", (), {"free": 900 * 1024**3, "total": 1000 * 1024**3, "used": 100 * 1024**3})(),
        }

        def mock_disk_usage(p):
            key = str(p).replace("/", "\\")
            for k, v in fake_usage.items():
                if key.lower().startswith(k.lower()):
                    return v
            raise OSError("no disk")

        with patch("folder_compare.shutil.disk_usage", side_effect=mock_disk_usage):
            cands = get_compare_temp_dir_candidates(1024)
        assert cands
        first = str(cands[0]).lower()
        assert "svinayak01" in first
        remote_idx = next(
            (i for i, p in enumerate(cands) if "inpnqbdnc04" in str(p).lower()),
            None,
        )
        client_idx = next(
            (i for i, p in enumerate(cands) if "svinayak01" in str(p).lower()),
            None,
        )
        assert client_idx is not None and remote_idx is not None
        assert client_idx < remote_idx
    finally:
        clear_compare_client_context()


if __name__ == "__main__":
    test_resolve_client_machine_from_override()
    test_client_temp_unc_candidates()
    test_client_temp_skipped_for_localhost()
    test_estimate_space_floor()
    test_normalize_application_slug()
    test_is_compare_temp_entry_for_application()
    test_comparison_space_status_shape()
    test_disk_probe_unc_c_share()
    test_is_compare_temp_entry_name()
    with tempfile.TemporaryDirectory() as td:
        test_cleanup_compare_temp_before_run_local(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_cleanup_compare_temp_per_application(Path(td))
    test_candidate_priority_client_before_remote()
    print("All client temp path tests passed.")
