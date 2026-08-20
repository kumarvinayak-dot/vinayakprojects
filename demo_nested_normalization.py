#!/usr/bin/env python3
"""
Demonstration script showing how nested JAR paths are normalized.
This shows exactly how the CRM-specific naming patterns are handled.
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import ArchiveContentsComparer


def demo_nested_normalization():
    """Demonstrate normalization of nested JAR paths."""
    
    comparer = ArchiveContentsComparer("test1", "test2")
    
    # Example nested paths from the user's scenario
    nested_paths = [
        # Path in HFB22
        "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/war/CRM/winpack__V10_0_1_22.jar",
        # Path in HFB21 (would be similar)
        "crm/CRM_10.0/CRM10.0.1_ALL/modules/crm/core/SMARTWEB/war/CRM/winpack__V10_0_1_21.jar",
        
        # Unsigned JAR examples
        "modules/security/auth_unsigned.jar",
        "modules/security/auth.jar",
        
        # AmdocsCRM.ear examples
        "release/AmdocsCRM_10.0.1.22.ear",
        "release/AmdocsCRM_10.0.1.21.ear",
        
        # SDK CRM examples
        "SDK_CRM_10.0.1.22_linux64_20251120_2043.jar",
        "SDK_CRM_10.0.1.21_linux64_20250908_1434.jar",
    ]
    
    print("=" * 80)
    print("NESTED JAR PATH NORMALIZATION DEMONSTRATION")
    print("=" * 80)
    print()
    print("This demonstrates how nested JAR file paths are normalized to handle")
    print("version-specific naming differences between releases.")
    print()
    
    # Group pairs that should normalize to the same value
    pairs = [
        (nested_paths[0], nested_paths[1], "CRM winpack pattern"),
        (nested_paths[2], nested_paths[3], "Unsigned JAR pattern"),
        (nested_paths[4], nested_paths[5], "AmdocsCRM.ear pattern"),
        (nested_paths[6], nested_paths[7], "SDK CRM pattern"),
    ]
    
    for path1, path2, description in pairs:
        # Normalize the archive filenames
        normalized1 = comparer._normalize_inner_archive_marker(path1)
        normalized2 = comparer._normalize_inner_archive_marker(path2)
        
        matches = normalized1 == normalized2
        status = "[MATCH]" if matches else "[DIFFERENT]"
        
        print(f"{status} {description}")
        print(f"  HFB22 path: {path1}")
        print(f"  Normalized: {normalized1}")
        print()
        print(f"  HFB21 path: {path2}")
        print(f"  Normalized: {normalized2}")
        print()
        
        if matches:
            print("  Result: These paths will be treated as IDENTICAL during comparison")
        else:
            print("  Result: These paths will be reported as DIFFERENT")
        
        print()
        print("-" * 80)
        print()
    
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()
    print("The normalization ensures that version-specific naming differences")
    print("between HFB21 and HFB22 releases are handled correctly:")
    print()
    print("  1. Version patterns (__V10_0_1_22 vs __V10_0_1_21) are normalized")
    print("  2. Timestamps (_20251120_2043 vs _20250908_1434) are removed")
    print("  3. Embedded versions (_10.0.1.22_ vs _10.0.1.21_) are normalized")
    print("  4. Unsigned suffixes (_unsigned) are removed for matching")
    print()
    print("This allows the comparison tool to focus on ACTUAL content differences")
    print("rather than expected naming convention variations between releases.")
    print("=" * 80)


if __name__ == "__main__":
    demo_nested_normalization()

