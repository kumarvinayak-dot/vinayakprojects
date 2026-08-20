#!/usr/bin/env python3
"""Tests for CRM crm-playbooks Nexus URL building and YAML compare."""

import io
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from crm_playbooks_nexus import (
    build_crm_playbooks_tar_url,
    compare_crm_playbooks_yaml_in_amdocs_version,
)


def test_build_crm_playbooks_tar_url():
    urls = build_crm_playbooks_tar_url("10.24.1.7-4410")
    assert urls["tar"] == (
        "https://indlin2410:28081/nexus/content/repositories/"
        "crm-v10.24.1-Releases/com/amdocs/core/crm/crm-playbooks/"
        "10.24.1.7-4410/crm-playbooks-10.24.1.7-4410.tar"
    )
    urls2 = build_crm_playbooks_tar_url("10.24.1.6-4368")
    assert "10.24.1.6-4368" in urls2["tar"]


def test_compare_yaml_under_amdocs_version():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        prev = td_path / "prev.tar"
        latest = td_path / "latest.tar"

        def write_tar(path: Path, files: dict[str, str]):
            with tarfile.open(path, "w") as tf:
                for name, content in files.items():
                    data = content.encode("utf-8")
                    info = tarfile.TarInfo(name=name)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))

        write_tar(
            prev,
            {
                "crm-playbooks/amdocs-version/app/config.yaml": "key: old\n",
                "crm-playbooks/amdocs-version/removed.yaml": "x: 1\n",
                "crm-playbooks/other/ignore.yaml": "skip: true\n",
            },
        )
        write_tar(
            latest,
            {
                "crm-playbooks/amdocs-version/app/config.yaml": "key: new\n",
                "crm-playbooks/amdocs-version/added.yaml": "y: 2\n",
            },
        )
        diff = compare_crm_playbooks_yaml_in_amdocs_version(prev, latest)
        assert diff["has_changes"]
        assert "app/config.yaml" in [m["name"] for m in diff["modified"]]
        assert "removed.yaml" in diff["removed"]
        assert "added.yaml" in diff["added"]
        assert diff["total_previous"] == 2
        assert diff["total_latest"] == 2


if __name__ == "__main__":
    test_build_crm_playbooks_tar_url()
    test_compare_yaml_under_amdocs_version()
    print("OK: crm playbooks nexus tests passed")
