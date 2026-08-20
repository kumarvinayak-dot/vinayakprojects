#!/usr/bin/env python3
"""Tests for UXF/OMNI build-neutral archive alignment."""

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from uxf_compare import UXFComparer


def _make_war_with_lib(war_path: Path, jar_name: str, content: bytes = b"j") -> None:
    with zipfile.ZipFile(war_path, "w") as zf:
        zf.writestr(f"WEB-INF/lib/{jar_name}", content)


def test_build_token_neutral_keys_match_across_bn():
    c = UXFComparer("", "")
    old_path = (
        "v10.1_BN5256.zip/v10.1/server/rp-webapp/target/cm-server.war/"
        "WEB-INF/lib/AmdocsAif-10.0.0.pb01_hf11.jar"
    )
    new_path = (
        "v10.1_BN5249.zip/v10.1/server/rp-webapp/target/cm-server.war/"
        "WEB-INF/lib/AmdocsAif-10.0.0.pb01_hf11.jar"
    )
    assert c._uxf_archive_key(old_path, "AmdocsAif-10.0.0.pb01_hf11.jar") == c._uxf_archive_key(
        new_path, "AmdocsAif-10.0.0.pb01_hf11.jar"
    )


def test_version_jars_share_logical_key_but_differ_in_name():
    c = UXFComparer("", "")
    old_path = (
        "v10.1_BN5256.zip/v10.1/server/rp-webapp/target/cm-server.war/WEB-INF/lib/"
        "acpe-portfolio-project-10.1.0.26.772.jar"
    )
    new_path = (
        "v10.1_BN5249.zip/v10.1/server/rp-webapp/target/cm-server.war/WEB-INF/lib/"
        "acpe-portfolio-project-10.1.0.26.758.jar"
    )
    old_name = "acpe-portfolio-project-10.1.0.26.772.jar"
    new_name = "acpe-portfolio-project-10.1.0.26.758.jar"
    k_old = c._uxf_archive_key(old_path, old_name)
    k_new = c._uxf_archive_key(new_path, new_name)
    assert k_old == k_new
    assert old_name != new_name


def test_expanded_war_tree_indexes_leaf_jars(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_war = old_root / "v10.1_BN5256.zip" / "v10.1" / "server" / "rp-webapp" / "target" / "cm-server.war"
    new_war = new_root / "v10.1_BN5249.zip" / "v10.1" / "server" / "rp-webapp" / "target" / "cm-server.war"
    old_war.mkdir(parents=True)
    new_war.mkdir(parents=True)
    lib_old = old_war / "WEB-INF" / "lib"
    lib_new = new_war / "WEB-INF" / "lib"
    lib_old.mkdir(parents=True)
    lib_new.mkdir(parents=True)
    (lib_old / "AmdocsAif-10.0.0.pb01_hf11.jar").write_bytes(b"same")
    (lib_new / "AmdocsAif-10.0.0.pb01_hf11.jar").write_bytes(b"same")
    (lib_old / "acpe-portfolio-project-10.1.0.26.772.jar").write_bytes(b"old")
    (lib_new / "acpe-portfolio-project-10.1.0.26.758.jar").write_bytes(b"new")

    comparer = UXFComparer(str(old_root), str(new_root))
    comparer.compare_folders()

    assert "AmdocsAif-10.0.0.pb01_hf11.jar" not in " ".join(comparer.only_in_1)
    assert len(comparer.only_in_1) == 0, comparer.only_in_1
    assert len(comparer.only_in_2) == 0, comparer.only_in_2
    assert any(
        vc["old_name"].startswith("acpe-portfolio-project")
        for vc in comparer.version_changes
    ) or len(comparer.size_changes) == 1


def test_binary_war_scan_still_pairs_bn_builds(tmp_path):
    old_root = tmp_path / "old2"
    new_root = tmp_path / "new2"
    old_root.mkdir()
    new_root.mkdir()
    old_war = old_root / "cm-server.war"
    new_war = new_root / "cm-server.war"
    _make_war_with_lib(old_war, "AmdocsAif-10.0.0.pb01_hf11.jar", b"a")
    _make_war_with_lib(new_war, "AmdocsAif-10.0.0.pb01_hf11.jar", b"a")
    _make_war_with_lib(old_war, "acpe-portfolio-project-10.1.0.26.772.jar", b"1")
    # second jar added by rewriting war with both members
    with zipfile.ZipFile(old_war, "a") as zf:
        zf.writestr("WEB-INF/lib/extra-only-old.jar", b"x")
    with zipfile.ZipFile(new_war, "w") as zf:
        zf.writestr("WEB-INF/lib/AmdocsAif-10.0.0.pb01_hf11.jar", b"a")
        zf.writestr("WEB-INF/lib/acpe-portfolio-project-10.1.0.26.758.jar", b"2")

    comparer = UXFComparer(str(old_root), str(new_root))
    comparer.compare_folders()

    assert len(comparer.only_in_1) == 1
    assert "extra-only-old.jar" in comparer.only_in_1[0]


def test_naming_side_by_side_rows_highlight_version_diff(tmp_path):
    old_root = tmp_path / "old3"
    new_root = tmp_path / "new3"
    old_war = old_root / "v10.1_BN5256.zip" / "v10.1" / "server" / "rp-webapp" / "target" / "cm-server.war"
    new_war = new_root / "v10.1_BN5249.zip" / "v10.1" / "server" / "rp-webapp" / "target" / "cm-server.war"
    old_war.mkdir(parents=True)
    new_war.mkdir(parents=True)
    lib_old = old_war / "WEB-INF" / "lib"
    lib_new = new_war / "WEB-INF" / "lib"
    lib_old.mkdir(parents=True)
    lib_new.mkdir(parents=True)
    (lib_old / "acpe-portfolio-project-10.1.0.26.772.jar").write_bytes(b"old")
    (lib_new / "acpe-portfolio-project-10.1.0.26.758.jar").write_bytes(b"new")
    (lib_old / "extra-old-only.jar").write_bytes(b"x")

    comparer = UXFComparer(str(old_root), str(new_root))
    comparer.compare_folders()
    rows = comparer._uxf_build_naming_alignment_rows()
    naming_diffs = [r for r in rows if r.get("status") == "Different naming convention"]
    assert len(naming_diffs) == 1
    assert any(r.get("orphan_side") == "old" for r in rows if r.get("status") == "Only in old build")
    base = comparer._uxf_report_compare_base_path()
    assert "WEB-INF" in base.upper()
    assert "{BN}" in base
    assert "cm-server.war" in base
    html_report = comparer.generate_html_report()
    assert "jar-naming-side-by-side" in html_report
    assert "Compare location (shown once)" in html_report
    assert "acpe-portfolio-project" in html_report


def test_identical_lib_jar_not_listed_or_highlighted(tmp_path):
    """JARs with the same name on both builds (e.g. a3s-cache-api) must not appear in naming report."""
    old_root = tmp_path / "old4"
    new_root = tmp_path / "new4"
    old_war = (
        old_root / "v10.1_BN5256.zip" / "v10.1" / "server" / "rp-webapp" / "target" / "cm-server.war"
    )
    new_war = (
        new_root / "v10.1_BN5249.zip" / "v10.1" / "server" / "rp-webapp" / "target" / "cm-server.war"
    )
    old_war.mkdir(parents=True)
    new_war.mkdir(parents=True)
    lib_old = old_war / "WEB-INF" / "lib"
    lib_new = new_war / "WEB-INF" / "lib"
    lib_old.mkdir(parents=True)
    lib_new.mkdir(parents=True)
    jar_name = "a3s-cache-api-10.0.0.pb01.jar"
    (lib_old / jar_name).write_bytes(b"same")
    (lib_new / jar_name).write_bytes(b"same")
    (lib_old / "acpe-portfolio-project-10.1.0.26.772.jar").write_bytes(b"old")
    (lib_new / "acpe-portfolio-project-10.1.0.26.758.jar").write_bytes(b"new")

    comparer = UXFComparer(str(old_root), str(new_root))
    comparer.compare_folders()
    rows = comparer._uxf_build_naming_alignment_rows()
    assert jar_name not in " ".join(
        str(r.get("old_leaf") or r.get("old_name") or "") + str(r.get("new_leaf") or r.get("new_name") or "")
        for r in rows
        if r.get("status") == "Different naming convention"
    )
    naming_diffs = [r for r in rows if r.get("status") == "Different naming convention"]
    assert len(naming_diffs) == 1
    assert "acpe-portfolio-project" in naming_diffs[0]["old_leaf"]
    present = [r for r in rows if r.get("status") == "Present in both" and jar_name in str(r.get("old_leaf") or "")]
    assert len(present) == 1
    html_report = comparer.generate_html_report()
    assert "JARs with different naming convention" in html_report
    assert "Present in both" in html_report
    assert jar_name in html_report or "acpe-portfolio-project" in html_report


def test_acpe_direct_lib_paths_as_compare_root(root: Path):
    """User enters WEB-INF/lib as both Old and New build paths (BN5249 vs BN5256)."""
    def _lib_root(bn: str) -> Path:
        return (
            root
            / f"v10.1_{bn}"
            / "All"
            / f"v10.1_{bn}.zip"
            / "v10.1"
            / "server"
            / "rp-webapp"
            / "target"
            / "cm-server.war"
            / "WEB-INF"
            / "lib"
        )

    old_lib = _lib_root("BN5249")
    new_lib = _lib_root("BN5256")
    old_lib.mkdir(parents=True)
    new_lib.mkdir(parents=True)
    (old_lib / "acpe-portfolio-project-10.1.0.26.758.jar").write_bytes(b"old")
    (new_lib / "acpe-portfolio-project-10.1.0.26.772.jar").write_bytes(b"new")

    comparer = UXFComparer(str(old_lib), str(new_lib))
    comparer.compare_folders()

    assert comparer._uxf_lib_compare_mode()
    naming = [r for r in comparer._uxf_build_naming_alignment_rows() if r.get("status") == "Different naming convention"]
    assert len(naming) == 1, naming
    assert "758" in naming[0]["old_leaf"]
    assert "772" in naming[0]["new_leaf"]
    assert len(comparer.only_in_1) == 0, comparer.only_in_1
    assert len(comparer.only_in_2) == 0, comparer.only_in_2
    html_report = comparer.generate_html_report()
    assert "Different naming convention" in html_report
    assert "acpe-portfolio-project-10.1.0.26.758.jar" in html_report
    assert "acpe-portfolio-project-10.1.0.26.772.jar" in html_report


    assert "acpe-portfolio-project-10.1.0.26.772.jar" in html_report


def test_lib_naming_diffs_when_only_in_counts_zero(root: Path):
    """Matched lib jars with different leaf names must appear even when only-in counts are 0."""
    def _lib_root(bn: str) -> Path:
        return (
            root
            / "Daily"
            / bn
            / "All"
            / f"v10.1_{bn}.zip"
            / "v10.1"
            / "server"
            / "rp-webapp"
            / "target"
            / "cm-server.war"
            / "WEB-INF"
            / "lib"
        )

    old_lib = _lib_root("BN5249")
    new_lib = _lib_root("BN5256")
    old_lib.mkdir(parents=True, exist_ok=True)
    new_lib.mkdir(parents=True, exist_ok=True)
    (old_lib / "acpe-portfolio-project-10.1.0.26.758.jar").write_bytes(b"old")
    (new_lib / "acpe-portfolio-project-10.1.0.26.772.jar").write_bytes(b"new")

    comparer = UXFComparer(str(old_lib), str(new_lib))
    comparer.compare_folders()

    assert len(comparer.only_in_1) == 0
    assert len(comparer.only_in_2) == 0
    assert len(comparer.lib_jar_naming_diffs) == 1
    assert comparer.lib_jar_naming_diffs[0]["old_name"].endswith("758.jar")
    assert comparer.lib_jar_naming_diffs[0]["new_name"].endswith("772.jar")

    html = comparer.generate_html_report()
    assert "JAR Naming Diffs" in html
    assert "758" in html and "772" in html
    assert "No Differences Found" not in html


def test_acpe_cross_bn_paths_in_naming_table(root: Path):
    """All-folder roots (parent of zip tree) still surface acpe in the naming table."""
    def _lib_root(bn: str) -> Path:
        return (
            root
            / f"v10.1_{bn}"
            / "All"
            / f"v10.1_{bn}.zip"
            / "v10.1"
            / "server"
            / "rp-webapp"
            / "target"
            / "cm-server.war"
            / "WEB-INF"
            / "lib"
        )

    old_lib = _lib_root("BN5249")
    new_lib = _lib_root("BN5256")
    old_lib.mkdir(parents=True, exist_ok=True)
    new_lib.mkdir(parents=True, exist_ok=True)
    old_jar = old_lib / "acpe-portfolio-project-10.1.0.26.758.jar"
    new_jar = new_lib / "acpe-portfolio-project-10.1.0.26.772.jar"
    if not old_jar.exists():
        old_jar.write_bytes(b"old")
    if not new_jar.exists():
        new_jar.write_bytes(b"new")

    old_root = root / "v10.1_BN5249" / "All"
    new_root = root / "v10.1_BN5256" / "All"
    comparer = UXFComparer(str(old_root), str(new_root))
    comparer.compare_folders()

    naming = [r for r in comparer._uxf_build_naming_alignment_rows() if r.get("status") == "Different naming convention"]
    assert len(naming) == 1, naming
    assert "758" in naming[0]["old_leaf"]
    assert "772" in naming[0]["new_leaf"]


if __name__ == "__main__":
    test_build_token_neutral_keys_match_across_bn()
    test_version_jars_share_logical_key_but_differ_in_name()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_expanded_war_tree_indexes_leaf_jars(root)
        test_binary_war_scan_still_pairs_bn_builds(root)
        test_naming_side_by_side_rows_highlight_version_diff(root)
        test_identical_lib_jar_not_listed_or_highlighted(root)
        test_acpe_direct_lib_paths_as_compare_root(root)
        test_lib_naming_diffs_when_only_in_counts_zero(root)
        test_acpe_cross_bn_paths_in_naming_table(root)
    print("OK")
