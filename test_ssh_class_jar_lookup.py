#!/usr/bin/env python3
"""Unit tests for SSH class→JAR grep helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ssh_environment_validate import (  # noqa: E402
    _parse_grep_jar_paths,
    normalize_class_grep_pattern,
)


def test_normalize_class_grep_pattern():
    assert (
        normalize_class_grep_pattern("CreateCustomerAccountXB.class")
        == "CreateCustomerAccountXB"
    )
    assert (
        normalize_class_grep_pattern(
            "com.clarify.billingmanager.igpublic.savebean.CreateCustomerAccountXB"
        )
        == "com.clarify.billingmanager.igpublic.savebean.CreateCustomerAccountXB"
    )
    assert normalize_class_grep_pattern("  ") == ""


def test_parse_grep_jar_paths():
    root = "/opt/crm/deploy"
    out = "./lib/foo.jar\n./ear/nested/bar.jar\n"
    paths = _parse_grep_jar_paths(out, root)
    assert paths == [
        "/opt/crm/deploy/ear/nested/bar.jar",
        "/opt/crm/deploy/lib/foo.jar",
    ]


if __name__ == "__main__":
    test_normalize_class_grep_pattern()
    test_parse_grep_jar_paths()
    print("OK")
