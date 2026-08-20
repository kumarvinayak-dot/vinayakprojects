#!/usr/bin/env python3
"""Test if the normalization regex works correctly."""
import sys
import re
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import ArchiveContentsComparer

# Test file names from the comparison
test_names = [
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/unsigned_jars/ASCFUXFEnablement__V10_0_1_22.jar",
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/unsigned_jars/ASCFUXFEnablement__V10_0_1_21.jar",
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/unsigned_jars/ASMAdminFE__V10_0_1_22.jar",
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/unsigned_jars/ASMAdminFE__V10_0_1_21.jar",
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/war/CRM/ASCFUXFEnablement__V10_0_1_22.jar.pack.gz",
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/war/CRM/ASCFUXFEnablement__V10_0_1_21.jar.pack.gz",
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/war/CRM/ASMAdminFE__V10_0_1_22.jar.pack.gz",
    "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/war/CRM/ASMAdminFE__V10_0_1_21.jar.pack.gz",
]

comparer = ArchiveContentsComparer("dummy1", "dummy2")

print("Testing normalization on actual file paths:")
print("=" * 80)

for name in test_names:
    normalized = comparer._normalize_inner_archive_marker(name)
    print(f"Original:   {name}")
    print(f"Normalized: {normalized}")
    print(f"Changed:    {name != normalized}")
    print()

