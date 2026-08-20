#!/usr/bin/env python3
"""Regression: ClfyAgent container rollup for added/removed nested JARs."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "src"))

from crm_compare import CRMComparisonResult, CRMFolderComparer


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_clfy_container_prefixes_from_class_paths():
    c = CRMFolderComparer("", "")
    prefixes = c._clfy_container_prefixes(
        "ClfyAgent.war!/WEB-INF/lib/removed.jar!/com/example/Foo.class"
    )
    assert "ClfyAgent.war" in prefixes
    assert "ClfyAgent.war!/WEB-INF/lib/removed.jar" in prefixes


def test_container_diff_from_member_sets():
    c = CRMFolderComparer("", "")
    names1 = {"lib/foo.jar!/com/a.class", "lib/bar.jar"}
    names2 = {"lib/bar.jar!/com/b.class"}
    diff = c._clfy_build_container_diff_from_members(names1, names2, [])
    assert "lib/foo.jar" in diff["container_only_old"], diff

    cidx = c._clfy_archive_diff_container_index(
        {
            "container_only_old": diff["container_only_old"],
            "container_only_new": diff["container_only_new"],
            "container_different": diff["container_different"],
        }
    )
    assert cidx["metrics_ext"][".jar"]["oo"] == 1
    assert len(cidx["rows"]) == 1


def test_clfy_container_index_omits_content_diff_by_default():
    c = CRMFolderComparer("", "")
    cidx = c._clfy_archive_diff_container_index(
        {
            "container_only_old": {"lib/removed.jar"},
            "container_only_new": {"lib/added.jar"},
            "container_different": {"lib/changed.jar"},
        }
    )
    status_keys = {r["status_key"] for r in cidx["rows"]}
    assert status_keys == {"only_old", "only_new"}
    assert cidx["metrics_ext"][".jar"]["df"] == 0
    assert not any(r["path"] == "lib/changed.jar" for r in cidx["rows"])

    cidx_all = c._clfy_archive_diff_container_index(
        {
            "container_only_old": set(),
            "container_only_new": set(),
            "container_different": {"lib/changed.jar"},
        },
        include_content_diff=True,
    )
    assert any(r["status_key"] == "different" for r in cidx_all["rows"])


def test_container_diff_detects_removed_nested_jar_via_class_expansion():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_zip = td_path / "old.zip"
        new_zip = td_path / "new.zip"

        inner_removed = io.BytesIO()
        with zipfile.ZipFile(inner_removed, "w") as ij:
            ij.writestr("com/example/Foo.class", b"class-bytes-old")

        _write_zip(
            old_zip,
            {
                "ClfyAgent.war": inner_removed.getvalue(),
                "lib/unchanged.jar": b"PK\x03\x04placeholder",
            },
        )
        _write_zip(
            new_zip,
            {
                "lib/unchanged.jar": b"PK\x03\x04placeholder",
            },
        )

        c = CRMFolderComparer(str(old_zip), str(new_zip))
        c.deep_nested_archives = True
        diff = c._compare_archive_contents(old_zip, new_zip, skip_unbalanced_shallow_rebalance=True)

        assert diff["container_only_old"], "expected removed WAR/JAR in container_only_old"
        assert any(
            "removed.jar" in p or "ClfyAgent.war" in p
            for p in diff["container_only_old"]
        )

        cidx = c._clfy_archive_diff_container_index(diff)
        assert cidx["metrics_ext"][".war"]["oo"] >= 1 or cidx["metrics_ext"][".jar"]["oo"] >= 1
        assert cidx["rows"], "report rows should list removed containers"


def test_multi_location_lib_clfyagent_only_on_new():
    """New SDK lib/ClfyAgent.zip additions must appear even when sharedlib/ClfyAgent.zip exists on both."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_sdk = td_path / "old_sdk.jar"
        new_sdk = td_path / "new_sdk.jar"

        shared_inner = io.BytesIO()
        with zipfile.ZipFile(shared_inner, "w") as zf:
            zf.writestr("readme.txt", b"sharedlib bundle")

        lib_inner = io.BytesIO()
        with zipfile.ZipFile(lib_inner, "w") as zf:
            zf.writestr("spring-aop-6.2.16.jar", b"PK\x03\x04spring-aop")
            zf.writestr("spring-tx.jar", b"PK\x03\x04spring-tx")

        base = "crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/lib"
        _write_zip(
            old_sdk,
            {f"{base}/sharedlib/ClfyAgent.zip": shared_inner.getvalue()},
        )
        _write_zip(
            new_sdk,
            {
                f"{base}/sharedlib/ClfyAgent.zip": shared_inner.getvalue(),
                f"{base}/ClfyAgent.zip": lib_inner.getvalue(),
            },
        )

        c = CRMFolderComparer(str(old_sdk), str(new_sdk))
        c.compare_clfy_agent_zip = True
        c.deep_nested_archives = True
        info1 = c._synthetic_clfy_agent_info(old_sdk)
        info2 = c._synthetic_clfy_agent_info(new_sdk)
        assert info1 and info2
        diff = c._compare_clfy_all_bundle_locations(info1, info2, info1, info2)
        only_new = diff.get("container_only_new") or set()
        only_new_s = " ".join(only_new).lower()
        assert "spring-aop" in only_new_s or "spring-tx" in only_new_s, diff
        assert diff.get("summary_only_new", len(only_new)) >= 1 or len(only_new) >= 1


def test_jar_mismatch_details_dedupe_same_path():
    c = CRMFolderComparer("", "")
    dup_path = "sdk.jar!/crm/lib/ClfyAgent.zip!/spring-aop.jar"
    old = [dup_path] * 70
    new: list[str] = []
    u_old, u_new = c._pair_archive_paths_by_signature(old, new)
    assert len(u_old) == 1, f"expected 1 unique path, got {len(u_old)}"
    assert u_old[0] == dup_path
    assert u_new == []


def test_war_slash_vs_bang_normalizes_identical_class():
    c = CRMFolderComparer("", "")
    slash = (
        "ClfyAgent.war/WEB-INF/lib/gson.jar!/com/google/gson/internal/"
        "$Gson$Preconditions.class"
    )
    bang = (
        "ClfyAgent.war!/WEB-INF/lib/gson.jar!/com/google/gson/internal/"
        "$Gson$Preconditions.class"
    )
    assert c._member_match_key(slash) == c._member_match_key(bang)


def test_merge_clfy_drops_identical_inner_class_from_both_sides():
    c = CRMFolderComparer("", "")
    tail = (
        "ClfyAgent.war!/WEB-INF/lib/gson.jar!/com/google/gson/internal/"
        "$Gson$Preconditions.class"
    )
    host = "crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/lib/ClfyAgent.ear"
    d1 = {"only_in_old": {f"{host}!/{tail}"}, "only_in_new": set()}
    d2 = {
        "only_in_old": set(),
        "only_in_new": {
            f"{host}!/{tail.replace('war!/', 'war/')}"
        },
    }
    merged = c._merge_clfy_archive_diffs([d1, d2])
    assert not merged.get("only_in_old"), merged
    assert not merged.get("only_in_new"), merged


def test_inner_member_report_skips_identical_gson_class():
    c = CRMFolderComparer("", "")
    tail = (
        "ClfyAgent.war/WEB-INF/lib/gson.jar!/com/google/gson/internal/"
        "$Gson$Preconditions.class"
    )
    host = "crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/lib/ClfyAgent.ear"
    c.clfy_agent_zip_diff = {
        "archive_diff": {
            "only_in_old": {f"{host}!/{tail}"},
            "only_in_new": {
                f"{host}!/{tail.replace('war/', 'war!/')}"
            },
        }
    }
    assert c._collect_inner_jar_member_mismatches([], side="old") == []
    assert c._collect_inner_jar_member_mismatches([], side="new") == []


def test_drop_implicit_meta_inf_maven_directory_markers():
    c = CRMFolderComparer("", "")
    jar = "AmdocsCihxCrmServicesEjbDesc.jar"
    pom = (
        f"{jar}!/META-INF/maven/com.amdocs.crm/"
        f"AmdocsCihxCrmServicesEjbDesc/pom.xml"
    )
    normalized1 = {
        c._normalize_inner_archive_marker(f"{jar}!/META-INF/maven"),
        c._normalize_inner_archive_marker(
            f"{jar}!/META-INF/maven/com.amdocs.crm"
        ),
        c._normalize_inner_archive_marker(pom),
    }
    normalized2 = {c._normalize_inner_archive_marker(pom)}
    common = normalized1 & normalized2
    only_old = normalized1 - normalized2
    dropped = c._drop_implicit_directory_only_members(
        only_old, normalized2, common
    )
    assert c._normalize_inner_archive_marker(pom) in common
    assert dropped == set(), f"expected empty, got {dropped}"


def test_inner_member_report_skips_meta_inf_maven_on_both_builds():
    c = CRMFolderComparer("", "")
    jar = "AmdocsCihxCrmServicesEjbDesc.jar"
    host = "crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/lib/ClfyAgent.ear"
    pom = (
        f"{jar}!/META-INF/maven/com.amdocs.crm/"
        f"AmdocsCihxCrmServicesEjbDesc/pom.xml"
    )
    pom_props = (
        f"{jar}!/META-INF/maven/com.amdocs.crm/"
        f"AmdocsCihxCrmServicesEjbDesc/pom.properties"
    )
    sig_pom = c._jar_inner_file_signature(pom)
    sig_props = c._jar_inner_file_signature(pom_props)
    c.clfy_agent_zip_diff = {
        "archive_diff": {
            "only_in_old": {
                f"{jar}!/META-INF/maven",
                f"{jar}!/META-INF/maven/com.amdocs.crm",
                f"{jar}!/META-INF/maven/com.amdocs.crm/AmdocsCihxCrmServicesEjbDesc",
                pom,
                pom_props,
            },
            "only_in_new": set(),
            "all_member_sig_keys_old": {sig_pom, sig_props},
            "all_member_sig_keys_new": {sig_pom, sig_props},
        }
    }
    rows = c._collect_inner_jar_member_mismatches([], side="old")
    assert rows == [], rows


def test_merge_clfy_drops_meta_inf_when_pom_on_both_sides():
    c = CRMFolderComparer("", "")
    jar = "AmdocsCihxCrmServicesEjbDesc.jar"
    host = "crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/lib/ClfyAgent.ear"
    pom = (
        f"{jar}!/META-INF/maven/com.amdocs.crm/"
        f"AmdocsCihxCrmServicesEjbDesc/pom.xml"
    )
    sig_pom = c._jar_inner_file_signature(pom)
    d1 = {
        "only_in_old": {
            f"{host}!/{jar}!/META-INF/maven",
            f"{host}!/{pom}",
        },
        "only_in_new": set(),
        "all_member_sig_keys_old": {sig_pom},
        "all_member_sig_keys_new": {sig_pom},
    }
    d2 = {
        "only_in_old": set(),
        "only_in_new": {f"{host}!/{jar}!/META-INF/maven/com.amdocs.crm"},
        "all_member_sig_keys_old": {sig_pom},
        "all_member_sig_keys_new": {sig_pom},
    }
    merged = c._merge_clfy_archive_diffs([d1, d2])
    assert not merged.get("only_in_old"), merged
    assert not merged.get("only_in_new"), merged


def test_inner_members_separate_from_container_jar_mismatch():
    """Maven/class paths inside modified JARs must not inflate whole-container jar counts."""
    import tempfile
    import zipfile
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    jar_path = Path(tmp) / "spring-core.jar"
    members = [
        "META-INF/maven/org.springframework/spring-core/pom.properties",
        "META-INF/maven/org.springframework/spring-core/pom.xml",
        "org/springframework/core/SpringVersion.class",
    ]
    with zipfile.ZipFile(jar_path, "w") as zf:
        for m in members:
            zf.writestr(m, b"x")

    c = CRMFolderComparer(str(tmp), str(tmp))
    modified = [
        CRMComparisonResult(
            relative_path="lib/spring-core.jar",
            status="modified",
            archive_diff={
                "only_in_old": members,
                "only_in_new": [],
                "all_member_sig_keys_old": {
                    c._jar_inner_file_signature(m) for m in members
                },
                "all_member_sig_keys_new": set(),
            },
        )
    ]
    # Place jar where _abs_pair_build_paths resolves it
    lib_dir = Path(tmp) / "lib"
    lib_dir.mkdir(exist_ok=True)
    jar_in_lib = lib_dir / "spring-core.jar"
    with zipfile.ZipFile(jar_in_lib, "w") as zf:
        for m in members:
            zf.writestr(m, b"x")

    inner_old = c._collect_inner_jar_member_mismatches(modified, side="old")
    assert len(inner_old) == 3, inner_old
    assert all(r["host"] == "lib/spring-core.jar" for r in inner_old)
    kinds = {r["kind"] for r in inner_old}
    assert "class" in kinds
    assert "file" in kinds

    old_paths = c._collect_java_archive_one_side_paths([], [], modified, side="old")
    new_paths = c._collect_java_archive_one_side_paths([], [], modified, side="new")
    assert old_paths == [], f"modified archive should not count as removed container: {old_paths}"
    assert new_paths == []

    d = c._count_delta_details([], [], modified)
    assert int(d["jar_delta"]) == 0
    assert len(d["inner_old_rows"]) == 3
    assert d["jar_rows"] == []

    display = c._jar_mismatch_display_rows(["lib/foo.jar", "other/foo.jar"])
    assert len(display) == 2
    assert display[0]["name"] == "foo.jar"
    assert display[0]["path"] == "lib/foo.jar"
    assert display[1]["path"] == "other/foo.jar"


def test_compare_archive_drops_phantom_and_unverified_only_in_new():
    import tempfile
    import zipfile
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    old_jar = Path(tmp) / "old.jar"
    new_jar = Path(tmp) / "new.jar"
    real_class = "com/example/Real.class"
    with zipfile.ZipFile(old_jar, "w") as zf:
        pass
    with zipfile.ZipFile(new_jar, "w") as zf:
        zf.writestr(real_class, b"\xca\xfe\xba\xbe")

    c = CRMFolderComparer(str(tmp), str(tmp))
    c.deep_nested_archives = True
    diff = c._compare_archive_contents(old_jar, new_jar)
    only_new = set(diff.get("only_in_new") or [])
    assert real_class in {c._normalize_inner_archive_marker(p) for p in only_new} or any(
        real_class in str(p) for p in only_new
    ), only_new
    assert not any("[error-reading" in str(p).lower() for p in only_new), only_new


def test_inner_member_report_skips_phantom_only_in_new():
    c = CRMFolderComparer("", "")
    modified = [
        CRMComparisonResult(
            relative_path="lib/app.jar",
            status="modified",
            archive_diff={
                "only_in_old": set(),
                "only_in_new": {
                    "lib/inner.jar!/[error-reading-inner:BadZipFile]",
                    "com/phantom/NotInJar.class",
                },
            },
        )
    ]
    rows = c._collect_inner_jar_member_mismatches(modified, side="new")
    assert rows == [], rows


def test_compare_archive_drops_cross_build_class_signatures():
    import tempfile
    import zipfile
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    cls = "WEB-INF/classes/com/foo/Bar.class"
    jar1 = Path(tmp) / "a.jar"
    jar2 = Path(tmp) / "b.jar"
    with zipfile.ZipFile(jar1, "w") as zf:
        zf.writestr(f"inner.war!/{cls}", b"old")
    with zipfile.ZipFile(jar2, "w") as zf:
        zf.writestr(f"inner.war/{cls}", b"new")

    c = CRMFolderComparer(str(tmp), str(tmp))
    c.deep_nested_archives = False
    diff = c._compare_archive_contents(jar1, jar2)
    assert not diff.get("only_in_old"), diff.get("only_in_old")
    assert not diff.get("only_in_new"), diff.get("only_in_new")


def test_clfy_size_only_detects_version_renamed_jar():
    """Version-renamed JARs (same logical name, different suffix) appear in version_mismatch_rows."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_zip = td_path / "old.zip"
        new_zip = td_path / "new.zip"

        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as ij:
            ij.writestr("com/example/Same.class", b"same")

        war_old = io.BytesIO()
        with zipfile.ZipFile(war_old, "w") as wz:
            wz.writestr(
                "WEB-INF/lib/AmdocsCRM-BM-Collection__V10_24_1_0.jar",
                inner.getvalue(),
            )

        war_new = io.BytesIO()
        with zipfile.ZipFile(war_new, "w") as wz:
            wz.writestr(
                "WEB-INF/lib/AmdocsCRM-BM-Collection__V10_25.jar",
                inner.getvalue(),
            )

        _write_zip(old_zip, {"ClfyAgent.war": war_old.getvalue()})
        _write_zip(new_zip, {"ClfyAgent.war": war_new.getvalue()})

        c = CRMFolderComparer(str(old_zip), str(new_zip))
        diff = c._compare_archive_contents_clfy_size_only(old_zip, new_zip)
        vm = diff.get("version_mismatch_rows") or []
        assert vm, diff
        assert any(
            "BM-Collection" in str(r.get("logical_name") or "") for r in vm
        ), vm
        html = c._html_clfy_bc_container_diff_table(diff)
        assert "Version mismatch" in html
        assert "#cce8ff" in html


def test_clfy_size_only_detects_jar_size_change():
    """Size-only ClfyAgent compare flags JARs whose zip entry size differs (no .class scan)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_zip = td_path / "old.ear"
        new_zip = td_path / "new.ear"

        inner_same = io.BytesIO()
        with zipfile.ZipFile(inner_same, "w") as ij:
            ij.writestr("com/example/Unchanged.class", b"same-bytes")

        inner_changed_old = io.BytesIO()
        with zipfile.ZipFile(inner_changed_old, "w") as ij:
            ij.writestr("com/example/Changed.class", b"old-bytes" + b"x" * 400)

        inner_changed_new = io.BytesIO()
        with zipfile.ZipFile(inner_changed_new, "w") as ij:
            ij.writestr("com/example/Changed.class", b"new-bytes" + b"y" * 800)

        war_old = io.BytesIO()
        with zipfile.ZipFile(war_old, "w") as wz:
            wz.writestr("WEB-INF/lib/unchanged.jar", inner_same.getvalue())
            wz.writestr("WEB-INF/lib/changed.jar", inner_changed_old.getvalue())

        war_new = io.BytesIO()
        with zipfile.ZipFile(war_new, "w") as wz:
            wz.writestr("WEB-INF/lib/unchanged.jar", inner_same.getvalue())
            wz.writestr("WEB-INF/lib/changed.jar", inner_changed_new.getvalue())

        _write_zip(old_zip, {"ClfyAgent.war": war_old.getvalue()})
        _write_zip(new_zip, {"ClfyAgent.war": war_new.getvalue()})

        c = CRMFolderComparer(str(old_zip), str(new_zip))
        diff = c._compare_archive_contents_clfy_size_only(old_zip, new_zip)

        assert diff.get("size_compare_mode")
        assert diff.get("container_different"), diff
        assert any(
            "changed.jar" in str(p).lower() for p in (diff.get("container_different") or set())
        )
        rows = diff.get("jar_size_diff_rows") or []
        assert any("changed.jar" in str(r.get("path", "")).lower() for r in rows), rows
        assert not diff.get("class_files_changed"), "size-only mode must not list .class changes"


def test_clfy_size_only_detects_removed_war():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_zip = td_path / "old.zip"
        new_zip = td_path / "new.zip"
        inner_removed = io.BytesIO()
        with zipfile.ZipFile(inner_removed, "w") as ij:
            ij.writestr("com/example/Foo.class", b"class-bytes-old")
        _write_zip(old_zip, {"ClfyAgent.war": inner_removed.getvalue()})
        _write_zip(new_zip, {"lib/placeholder.jar": b"PK\x03\x04placeholder"})

        c = CRMFolderComparer(str(old_zip), str(new_zip))
        diff = c._compare_archive_contents_clfy_size_only(old_zip, new_zip)
        assert diff.get("container_only_old"), diff
        assert any(
            "ClfyAgent.war" in str(p) for p in (diff.get("container_only_old") or set())
        )


def test_clfy_outer_read_cache_avoids_repeat_copy():
    """Repeated ClfyAgent outer-archive reads must not copy the same file multiple times."""
    import shutil

    real_copy2 = shutil.copy2
    with tempfile.TemporaryDirectory() as td:
        outer = Path(td) / "crm_clfy_agent_test.ear"
        _write_zip(outer, {"nested.jar": b"PK\x03\x04"})
        c = CRMFolderComparer("", "")
        copy_count = 0

        def _counting_copy(src, dst):
            nonlocal copy_count
            copy_count += 1
            return real_copy2(src, dst)

        with patch("crm_compare.shutil.copy2", side_effect=_counting_copy):
            p1 = c._copy_file_to_temp_for_clfy_read(outer)
            p2 = c._copy_file_to_temp_for_clfy_read(outer)
            p3 = c._resolve_outer_archive_for_zip(outer)
        assert p1 is not None and p2 is not None and p3 is not None
        assert p1 == p2 == p3
        assert copy_count == 0, f"expected in-place ZipFile read, got {copy_count} copy(ies)"


def test_clfy_outer_read_cache_single_copy_when_zipfile_unavailable():
    """When ZipFile cannot read in place, copy once and reuse the cached temp path."""
    import shutil

    real_copy2 = shutil.copy2
    with tempfile.TemporaryDirectory() as td:
        outer = Path(td) / "crm_clfy_agent_big.ear"
        _write_zip(outer, {"nested.jar": b"payload"})
        c = CRMFolderComparer("", "")
        copy_count = 0

        def _counting_copy(src, dst):
            nonlocal copy_count
            copy_count += 1
            return real_copy2(src, dst)

        with patch("crm_compare.CRMFolderComparer._zipfile_opens_ok", return_value=False):
            with patch("crm_compare.shutil.copy2", side_effect=_counting_copy):
                p1 = c._copy_file_to_temp_for_clfy_read(outer)
                p2 = c._copy_file_to_temp_for_clfy_read(outer)
        assert p1 is not None and p2 is not None
        assert p1 == p2
        assert copy_count == 1, f"expected exactly one copy, got {copy_count}"


def test_find_jars_containing_class_in_nested_jar():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as ij:
            ij.writestr(
                "com/amdocs/crm/CreateCustomerAccountXB.class",
                b"class-bytes",
            )
        sdk = td_path / "SDK_CRM.jar"
        _write_zip(sdk, {"lib/ClfyBillingManager.jar": inner.getvalue()})
        c = CRMFolderComparer(str(sdk), str(sdk))
        result = c.find_jars_containing_class(sdk, "CreateCustomerAccountXB")
        assert result.get("ok"), result
        assert "ClfyBillingManager.jar" in (result.get("suggestions") or ""), result


def test_clfy_shallow_discovery_finds_top_level_bundle():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        sdk = td_path / "SDK.jar"
        clfy = io.BytesIO()
        with zipfile.ZipFile(clfy, "w") as cz:
            cz.writestr("WEB-INF/lib/app.jar", b"PK\x03\x04")
        decoy = io.BytesIO()
        with zipfile.ZipFile(decoy, "w") as dz:
            dz.writestr("nested/ClfyAgent.zip", clfy.getvalue())
        _write_zip(
            sdk,
            {
                "crm/lib/ClfyAgent.zip": clfy.getvalue(),
                "crm/lib/decoy.jar": decoy.getvalue(),
            },
        )
        c = CRMFolderComparer(str(sdk), str(sdk))
        paths = c._list_clfy_paths_from_outer_archive(sdk)
        assert any(p.endswith("crm/lib/ClfyAgent.zip") for p in paths), paths
        assert not any("decoy.jar!/" in p for p in paths), paths


def test_foundation_jar_skips_inner_class_scan_by_default():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_outer = td_path / "sdk_old.jar"
        new_outer = td_path / "sdk_new.jar"

        inner_old = io.BytesIO()
        with zipfile.ZipFile(inner_old, "w") as ij:
            ij.writestr("com/foo/Old.class", b"old")
        inner_new = io.BytesIO()
        with zipfile.ZipFile(inner_new, "w") as ij:
            ij.writestr("com/foo/New.class", b"new")

        _write_zip(old_outer, {"lib/AmdocsCore.jar": inner_old.getvalue()})
        _write_zip(new_outer, {"lib/AmdocsCore.jar": inner_new.getvalue()})

        c = CRMFolderComparer(str(old_outer), str(new_outer))
        c.deep_nested_archives = True
        names1, _ = c._get_archive_members(old_outer)
        names2, _ = c._get_archive_members(new_outer)

        assert "lib/AmdocsCore.jar" in names1
        assert "lib/AmdocsCore.jar" in names2
        assert not [
            n for n in names1 if "AmdocsCore.jar!/" in n and n.lower().endswith(".class")
        ], names1
        assert not [
            n for n in names2 if "AmdocsCore.jar!/" in n and n.lower().endswith(".class")
        ], names2


def test_foundation_jar_class_scan_when_named_in_text_box():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_outer = td_path / "sdk_old.jar"
        new_outer = td_path / "sdk_new.jar"

        inner_old = io.BytesIO()
        with zipfile.ZipFile(inner_old, "w") as ij:
            ij.writestr("com/foo/Old.class", b"old")
        inner_new = io.BytesIO()
        with zipfile.ZipFile(inner_new, "w") as ij:
            ij.writestr("com/foo/New.class", b"new")

        _write_zip(old_outer, {"lib/AmdocsCore.jar": inner_old.getvalue()})
        _write_zip(new_outer, {"lib/AmdocsCore.jar": inner_new.getvalue()})

        c = CRMFolderComparer(str(old_outer), str(new_outer))
        c.class_diff_jar_name = "AmdocsCore.jar"
        c.deep_nested_archives = True
        names1, _ = c._get_archive_members(old_outer)
        inner_old_paths = [
            n for n in names1 if "AmdocsCore.jar!/" in n and n.lower().endswith(".class")
        ]
        assert len(inner_old_paths) == 1, names1


def test_crm_expected_new_path_maps_release_dir_from_pkg_pair():
    """New-build column must use CRM_10.25, not CRM_10.24/CRM10.25.0_ALL hybrid."""
    c = CRMFolderComparer("/old", "/new")
    c.results = [
        CRMComparisonResult(
            relative_path=(
                "crm/CRM_10.24/CRM10.24.1_ALL/lib/OnlyInOld.jar"
                " -> "
                "crm/CRM_10.24/CRM10.25.0_ALL/lib/OnlyInOld.jar"
            ),
            status="modified",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/lib/OnlyInOld.jar",
            status="removed",
        ),
    ]
    old_path = "crm/CRM_10.24/CRM10.24.1_ALL/lib/OnlyInOld.jar"
    mapped = c._crm_expected_new_path_for_old(old_path)
    assert mapped == "crm/CRM_10.25/CRM10.25.0_ALL/lib/absent", mapped


def test_path_count_mismatch_excludes_release_renamed_pairs():
    """Files present on both builds (release folder rename only) are not path orphans."""
    c = CRMFolderComparer(
        "crm/CRM_10.24/CRM10.24.1_ALL",
        "crm/CRM_10.25/CRM10.25.0_ALL",
    )
    c.results = [
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/someproj/.classpath",
            status="removed",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.25/CRM10.25.0_ALL/someproj/.classpath",
            status="added",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/lib/a3s-cache-api.jar",
            status="removed",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.25/CRM10.25.0_ALL/lib/a3s-cache-api.jar",
            status="added",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/someproj/.project",
            status="removed",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.25/CRM10.25.0_ALL/someproj/.project",
            status="added",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/lib/removed-only.jar",
            status="removed",
        ),
    ]
    added = [r for r in c.results if r.status == "added"]
    removed = [r for r in c.results if r.status == "removed"]
    modified = [r for r in c.results if r.status == "modified"]
    counts = c._side_by_side_counts(added, removed, modified, 0)
    assert counts["files_only_old"] == 1, counts
    assert counts["files_only_new"] == 0, counts
    delta = c._count_delta_details(added, removed, modified)
    assert delta["path_delta"] == 1, delta
    orphan_names = {r["name"] for r in delta["path_rows"]}
    assert orphan_names == {"removed-only.jar"}, orphan_names


def test_missing_jar_summaries_exclude_version_renamed_pair():
    """Removed+added JAR with same logical name must not appear in missing JAR list."""
    c = CRMFolderComparer("/old", "/new")
    c.results = [
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/lib/AmdocsCRM-BM-Collection__V10_24_1_0.jar",
            status="removed",
        ),
        CRMComparisonResult(
            relative_path="crm/CRM_10.25/CRM10.25.0_ALL/lib/AmdocsCRM-BM-Collection__V10_25.jar",
            status="added",
        ),
    ]
    missing = c._collect_missing_jar_summaries()
    names = {m["logical_name"].lower() for m in missing}
    assert "bm-collection" not in names and "amdocscrm-bm-collection" not in names, missing
    ver = c._collect_version_changed_jar_summaries()
    assert any("BM-Collection" in v["logical_name"] for v in ver), ver


def test_crm_sdk_jar_roots_infer_release_dir_without_results():
    """Cross-release SDK .jar compare must map CRM_10.24 → CRM_10.25 from jar paths alone."""
    old_sdk = (
        r"\\ntnet\indiafilestore1\cm_share2\v10.24.1\v10.24.1_68\SDK\Output"
        r"\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar"
    )
    new_sdk = (
        r"\\ntnet\indiafilestore1\cm_share2\v10.25\v10.25_13\SDK\Output"
        r"\SDK_CRM_10.25_linux64_20260505_0812.jar"
    )
    c = CRMFolderComparer(old_sdk, new_sdk)
    c.results = []
    old_path = "crm/CRM_10.24/CRM10.24.1_ALL/lib/Example.jar"
    mapped = c._crm_expected_new_path_for_old(old_path)
    assert mapped == "crm/CRM_10.25/CRM10.25.0_ALL/lib/absent", mapped
    reps = c._crm_infer_release_replacements()
    assert ("CRM_10.24", "CRM_10.25") in reps, reps


def test_crm_sdk_modified_row_does_not_pollute_new_release_inference():
    """Modified rows without arrow must not treat old inner paths as new-build layout."""
    c = CRMFolderComparer("/old.jar", "/new.jar")
    c.results = [
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/lib/foo.jar",
            status="modified",
        ),
    ]
    reps = c._crm_infer_release_replacements()
    assert not reps, reps


def test_side_by_side_counts_use_scanned_tree_totals():
    """Total files in tree must reflect SDK scan size, not only diff result rows."""
    c = CRMFolderComparer("old.jar", "new.jar")
    c._crm_last_files1 = {"crm/CRM_10.24/CRM10.24.1_ALL/a": {}, "crm/CRM_10.24/CRM10.24.1_ALL/b": {}}
    c._crm_last_files2 = {
        "crm/CRM_10.25/CRM10.25.0_ALL/x": {},
        "crm/CRM_10.25/CRM10.25.0_ALL/y": {},
        "crm/CRM_10.25/CRM10.25.0_ALL/z": {},
    }
    c._crm_scan_file_count_old = 2
    c._crm_scan_file_count_new = 3
    c.results = [
        CRMComparisonResult(
            relative_path="crm/CRM_10.24/CRM10.24.1_ALL/a",
            status="removed",
        ),
    ]
    counts = c._side_by_side_counts([], c.results, [], 0)
    assert counts["total_old"] == 2, counts
    assert counts["total_new"] == 3, counts


def test_missing_jar_summary_skips_when_new_scan_has_counterpart():
    c = CRMFolderComparer("old.jar", "new.jar")
    old_p = "crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/lib/a3s-cache-api.jar"
    new_p = "crm/CRM_10.25/CRM10.25.0_ALL/modules/crm/lib/a3s-cache-api.jar"
    c._crm_last_files2 = {new_p: {"size": 1}}
    c._crm_scan_paths_for_inference = ({old_p: {}}, {new_p: {}})
    c._crm_scan_file_count_new = 1
    c.results = [CRMComparisonResult(relative_path=old_p, status="removed")]
    missing = c._collect_missing_jar_summaries()
    assert missing == [], missing
    assert c._crm_expected_new_path_for_old(old_p) == new_p


def test_collect_java_archive_paths_includes_modified_new_side():
    c = CRMFolderComparer("old.jar", "new.jar")
    old_p = "crm/CRM_10.24/CRM10.24.1_ALL/lib/foo.jar"
    new_p = "crm/CRM_10.25/CRM10.25.0_ALL/modules/crm/lib/foo.jar"
    c.results = [
        CRMComparisonResult(
            relative_path=f"{old_p} -> {new_p}",
            status="modified",
        ),
    ]
    new_jars = c._collect_java_archive_one_side_paths([], [], c.results, side="new")
    assert new_p in new_jars, new_jars


def test_crm_pairs_jar_relocated_from_lib_to_modules_crm_lib():
    """a3s-cache-api.jar under lib/ (old) must pair with modules/crm/lib/ (new), not absent."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_jar = td_path / "SDK_CRM_10.24.1.0_linux64.jar"
        new_jar = td_path / "SDK_CRM_10.25_linux64.jar"
        old_path = "crm/CRM_10.24/CRM10.24.1_ALL/lib/a3s-cache-api.jar"
        new_path = "crm/CRM_10.25/CRM10.25.0_ALL/modules/crm/lib/a3s-cache-api.jar"
        with zipfile.ZipFile(old_jar, "w") as zf:
            zf.writestr(old_path, b"old")
        with zipfile.ZipFile(new_jar, "w") as zf:
            zf.writestr(new_path, b"new")

        c = CRMFolderComparer(str(old_jar), str(new_jar))
        c.fast_mode = True
        c.compare_archive_contents = False
        c.run_full_tree_compare = True
        c.deep_nested_archives = False
        results = c.compare_folders()
        assert not any(r.status in ("removed", "added") for r in results), results
        expected = c._crm_expected_new_path_for_old(old_path)
        assert expected == new_path, expected
        assert "absent" not in expected.lower()
        rows = c._align_side_by_side_path_rows([old_path], [new_path])
        assert rows[0]["new_path"] == new_path, rows[0]


def test_path_delta_non_jar_lib_modules_relocation():
    """Non-JAR files under lib/ (old) must pair with modules/crm/lib/ (new) in path mismatch."""
    old_sdk = (
        r"\\DVCIISILON02\cm_share2\v10.24.1\v10.24.1_68\SDK\Output"
        r"\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar"
    )
    new_sdk = (
        r"\\DVCIISILON02\cm_share2\v10.25\v10.25_13\SDK\Output"
        r"\SDK_CRM_10.25_linux64_20260505_0812.jar"
    )
    c = CRMFolderComparer(old_sdk, new_sdk)
    old_p = "crm/CRM_10.24/CRM10.24.1_ALL/lib/config/app.xml"
    new_p = "crm/CRM_10.25/CRM10.25.0_ALL/modules/crm/lib/config/app.xml"
    c.results = [
        CRMComparisonResult(relative_path=old_p, status="removed"),
        CRMComparisonResult(relative_path=new_p, status="added"),
    ]
    added = [r for r in c.results if r.status == "added"]
    removed = [r for r in c.results if r.status == "removed"]
    pairs, ur, ua = c._crm_align_removed_added_for_report(removed, added)
    assert len(pairs) == 1 and not ur and not ua, (len(pairs), ur, ua)
    delta = c._count_delta_details(added, removed, [], align_cache=(pairs, ur, ua))
    assert delta["path_delta"] == 0, delta
    assert delta["path_rows"] == [], delta


def test_signature_bucket_align_bulk_release_renamed():
    """When greedy alignment budget is exceeded, signature buckets still pair release renames."""
    old_sdk = (
        r"\\DVCIISILON02\cm_share2\v10.24.1\v10.24.1_68\SDK\Output"
        r"\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar"
    )
    new_sdk = (
        r"\\DVCIISILON02\cm_share2\v10.25\v10.25_13\SDK\Output"
        r"\SDK_CRM_10.25_linux64_20260505_0812.jar"
    )
    c = CRMFolderComparer(old_sdk, new_sdk)
    n = 1100
    c.results = []
    for i in range(n):
        c.results.append(
            CRMComparisonResult(
                relative_path=f"crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/foo/file{i}.txt",
                status="removed",
            )
        )
        c.results.append(
            CRMComparisonResult(
                relative_path=f"crm/CRM_10.25/CRM10.25.0_ALL/modules/crm/foo/file{i}.txt",
                status="added",
            )
        )
    added = [r for r in c.results if r.status == "added"]
    removed = [r for r in c.results if r.status == "removed"]
    pairs, ur, ua = c._crm_align_removed_added_for_report(removed, added)
    assert len(pairs) == n and len(ur) == 0 and len(ua) == 0, (len(pairs), len(ur), len(ua))
    delta = c._count_delta_details(added, removed, [], align_cache=(pairs, ur, ua))
    assert delta["path_delta"] == 0, delta


def test_crm_sdk_cross_release_compare_pairs_inner_jars():
    """SDK v10.24.1 vs v10.25: inner JARs must pair across CRM release folders, not show as absent."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_jar = td_path / "SDK_CRM_10.24.1.0_linux64_20260608_0939.jar"
        new_jar = td_path / "SDK_CRM_10.25_linux64_20260505_0812.jar"
        old_path = "crm/CRM_10.24/CRM10.24.1_ALL/lib/SharedLib.jar"
        new_path = "crm/CRM_10.25/CRM10.25.0_ALL/lib/SharedLib.jar"
        with zipfile.ZipFile(old_jar, "w") as zf:
            zf.writestr(old_path, b"old-jar-bytes")
        with zipfile.ZipFile(new_jar, "w") as zf:
            zf.writestr(new_path, b"new-jar-bytes")

        c = CRMFolderComparer(str(old_jar), str(new_jar))
        c.fast_mode = True
        c.compare_archive_contents = False
        c.show_missing_only = False
        c.run_full_tree_compare = True
        c.deep_nested_archives = False
        results = c.compare_folders()
        assert not any(r.status in ("removed", "added") for r in results), [
            (r.status, r.relative_path) for r in results
        ]
        assert any(
            "SharedLib.jar" in str(r.relative_path)
            and "CRM_10.25" in str(r.relative_path)
            and " -> " in str(r.relative_path)
            for r in results
        ), results


def test_crm_sdk_cross_release_pairs_version_suffixed_jars():
    """JARs with __V10_24 vs __V10_25 suffix must pair across CRM release folders."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_jar = td_path / "SDK_CRM_10.24.1.0_linux64.jar"
        new_jar = td_path / "SDK_CRM_10.25_linux64.jar"
        old_path = (
            "crm/CRM_10.24/CRM10.24.1_ALL/modules/crm/core/SMARTWEB/unsigned_jars/"
            "comfyj__V10_24_1_0.jar"
        )
        new_path = (
            "crm/CRM_10.25/CRM10.25.0_ALL/modules/crm/core/SMARTWEB/unsigned_jars/"
            "comfyj__V10_25.jar"
        )
        with zipfile.ZipFile(old_jar, "w") as zf:
            zf.writestr(old_path, b"old")
        with zipfile.ZipFile(new_jar, "w") as zf:
            zf.writestr(new_path, b"new")

        c = CRMFolderComparer(str(old_jar), str(new_jar))
        c.fast_mode = True
        c.compare_archive_contents = False
        c.run_full_tree_compare = True
        c.deep_nested_archives = False
        results = c.compare_folders()
        assert not any(r.status in ("removed", "added") for r in results), results
        rows = c._align_side_by_side_path_rows([old_path], [new_path])
        assert len(rows) == 1, rows
        assert "absent" not in (rows[0].get("new_path") or "").lower(), rows[0]


def test_clfy_size_only_skips_matching_container_sizes():
    """Identical nested JAR zip entry sizes must not produce size-diff rows."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        old_zip = td_path / "old.zip"
        new_zip = td_path / "new.zip"
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as ij:
            ij.writestr("com/example/A.class", b"same-payload")
        inner_bytes = inner.getvalue()
        war = io.BytesIO()
        with zipfile.ZipFile(war, "w") as wz:
            wz.writestr("WEB-INF/lib/unchanged.jar", inner_bytes)
        war_bytes = war.getvalue()
        _write_zip(old_zip, {"ClfyAgent.war": war_bytes})
        _write_zip(new_zip, {"ClfyAgent.war": war_bytes})
        c = CRMFolderComparer(str(old_zip), str(new_zip))
        diff = c._compare_archive_contents_clfy_size_only(old_zip, new_zip)
        assert not diff.get("jar_size_diff_rows"), diff
        assert not diff.get("container_different"), diff


def test_crm_report_unchanged_count_without_storing_rows():
    """Unchanged files are counted, not stored — keeps report save fast on large trees."""
    c = CRMFolderComparer("/old", "/new")
    c.results = [
        CRMComparisonResult(relative_path="lib/a.jar", status="added", size_folder2=10),
        CRMComparisonResult(relative_path="lib/b.jar", status="removed", size_folder1=20),
    ]
    c.count_unchanged = 50_000
    c._crm_scan_file_count_old = 50_002
    c._crm_scan_file_count_new = 50_001
    added, removed, modified, unchanged_count = c._report_diff_buckets()
    assert len(added) == 1
    assert len(removed) == 1
    assert unchanged_count == 50_000
    assert len(c.results) == 2


if __name__ == "__main__":
    test_crm_expected_new_path_maps_release_dir_from_pkg_pair()
    test_side_by_side_counts_use_scanned_tree_totals()
    test_missing_jar_summary_skips_when_new_scan_has_counterpart()
    test_collect_java_archive_paths_includes_modified_new_side()
    test_crm_pairs_jar_relocated_from_lib_to_modules_crm_lib()
    test_crm_sdk_cross_release_compare_pairs_inner_jars()
    test_crm_sdk_cross_release_pairs_version_suffixed_jars()
    test_crm_sdk_jar_roots_infer_release_dir_without_results()
    test_crm_sdk_modified_row_does_not_pollute_new_release_inference()
    test_path_count_mismatch_excludes_release_renamed_pairs()
    test_missing_jar_summaries_exclude_version_renamed_pair()
    test_clfy_size_only_detects_version_renamed_jar()
    test_clfy_size_only_detects_jar_size_change()
    test_clfy_size_only_detects_removed_war()
    test_clfy_size_only_skips_matching_container_sizes()
    test_clfy_outer_read_cache_avoids_repeat_copy()
    test_clfy_outer_read_cache_single_copy_when_zipfile_unavailable()
    test_clfy_container_prefixes_from_class_paths()
    test_container_diff_from_member_sets()
    test_clfy_container_index_omits_content_diff_by_default()
    test_container_diff_detects_removed_nested_jar_via_class_expansion()
    test_multi_location_lib_clfyagent_only_on_new()
    test_jar_mismatch_details_dedupe_same_path()
    test_jar_mismatch_details_dedupe_same_path()
    test_war_slash_vs_bang_normalizes_identical_class()
    test_merge_clfy_drops_identical_inner_class_from_both_sides()
    test_inner_member_report_skips_identical_gson_class()
    test_drop_implicit_meta_inf_maven_directory_markers()
    test_inner_member_report_skips_meta_inf_maven_on_both_builds()
    test_merge_clfy_drops_meta_inf_when_pom_on_both_sides()
    test_inner_members_separate_from_container_jar_mismatch()
    test_compare_archive_drops_phantom_and_unverified_only_in_new()
    test_inner_member_report_skips_phantom_only_in_new()
    test_compare_archive_drops_cross_build_class_signatures()
    test_find_jars_containing_class_in_nested_jar()
    test_clfy_shallow_discovery_finds_top_level_bundle()
    test_foundation_jar_skips_inner_class_scan_by_default()
    test_foundation_jar_class_scan_when_named_in_text_box()
    print("OK: clfy container diff tests passed")
