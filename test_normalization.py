#!/usr/bin/env python3
"""
Test script to verify archive filename normalization for CRM JAR files.
This demonstrates that version-specific naming differences are now properly normalized.
"""
import sys
import re
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import ArchiveContentsComparer


def test_normalization():
    """Test the normalization function with CRM-specific patterns."""
    
    # Create a test comparer instance
    comparer = ArchiveContentsComparer("test1", "test2")
    
    # Test cases from the user's scenario
    test_cases = [
        # CRM winpack pattern
        ("winpack__V10_0_1_22.jar", "winpack.jar"),
        ("winpack__V10_0_1_21.jar", "winpack.jar"),
        
        # SDK CRM pattern
        ("SDK_CRM_10.0.1.22_linux64_20251120_2043.jar", "SDK_CRM_linux64.jar"),
        ("SDK_CRM_10.0.1.21_linux64_20250908_1434.jar", "SDK_CRM_linux64.jar"),
        
        # Unsigned JAR pattern
        ("module_unsigned.jar", "module.jar"),
        ("component-unsigned.jar", "component.jar"),
        
        # AmdocsCRM.ear pattern
        ("AmdocsCRM_10.0.1.22.ear", "AmdocsCRM.ear"),
        ("AmdocsCRM_10.0.1.21.ear", "AmdocsCRM.ear"),
        
        # Existing patterns (should still work)
        ("lightsaber-10.2.0.3-67024.war", "lightsaber.war"),
        ("abc_1.2.3-SNAPSHOT.jar", "abc.jar"),
        ("comp-build123.zip", "comp.zip"),
        
        # Edge cases
        ("CRM_10.0.1_ALL.jar", "CRM_ALL.jar"),
        ("test__V1_0_0.war", "test.war"),
    ]
    
    print("=" * 80)
    print("ARCHIVE FILENAME NORMALIZATION TEST")
    print("=" * 80)
    print()
    
    all_passed = True
    for original, expected in test_cases:
        normalized = comparer._normalize_archive_filename(original)
        passed = normalized == expected
        status = "[PASS]" if passed else "[FAIL]"
        
        if not passed:
            all_passed = False
        
        print(f"{status}")
        print(f"  Original:   {original}")
        print(f"  Expected:   {expected}")
        print(f"  Got:        {normalized}")
        print()
    
    print("=" * 80)
    if all_passed:
        print("ALL TESTS PASSED!")
        print()
        print("The normalization function will now properly handle:")
        print("  - CRM version patterns (__V10_0_1_22)")
        print("  - Embedded versions (10.0.1.22)")
        print("  - Unsigned JAR files (_unsigned)")
        print("  - Timestamps (_20251120_2043)")
        print("  - Standard version patterns (-1.2.3, _1.2.3)")
    else:
        print("SOME TESTS FAILED")
        return 1
    
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(test_normalization())

