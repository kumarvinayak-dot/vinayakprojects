#!/usr/bin/env python3
"""
Quick comparison script for CRM SDK JAR files with normalized naming conventions.
This script compares the two JAR files mentioned by the user and generates a detailed report.
"""
import sys
import os
from pathlib import Path
from datetime import datetime

# Add the src directory to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import FolderComparer, ArchiveContentsComparer


def compare_crm_sdk_jars():
    """Compare the two CRM SDK JAR files with normalization."""
    
    # The two JAR files to compare
    folder1 = r"\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB22_release\release\SDK_linux\SDK_CRM_10.0.1.22_linux64_20251120_2043.jar"
    folder2 = r"\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB21_release\release\SDK_linux\SDK_CRM_10.0.1.21_linux64_20250908_1434.jar"
    
    print("=" * 80)
    print("CRM SDK JAR COMPARISON WITH NORMALIZATION")
    print("=" * 80)
    print()
    print("Folder 1 (HFB22):")
    print(f"  {folder1}")
    print()
    print("Folder 2 (HFB21):")
    print(f"  {folder2}")
    print()
    print("Normalization enabled: YES")
    print("  - Version patterns (__V10_0_1_22) will be normalized")
    print("  - Timestamps (_20251120_2043) will be normalized")
    print("  - Embedded versions (_10.0.1.22_) will be normalized")
    print("  - Unsigned JAR suffixes (_unsigned) will be normalized")
    print()
    print("=" * 80)
    print()
    
    # Check if paths exist
    if not Path(folder1).exists():
        print(f"ERROR: Folder 1 does not exist or is not accessible: {folder1}")
        print()
        print("Possible solutions:")
        print("1. Verify network connectivity to \\\\DVCIISILON01")
        print("2. Check credentials/permissions for cm_share3")
        print("3. Map the network drive first:")
        print(f"   net use Z: \\\\DVCIISILON01\\cm_share3 /persistent:yes")
        return 1
    
    if not Path(folder2).exists():
        print(f"ERROR: Folder 2 does not exist or is not accessible: {folder2}")
        print()
        print("Possible solutions:")
        print("1. Verify network connectivity to \\\\DVCIISILON01")
        print("2. Check credentials/permissions for cm_share3")
        print("3. Map the network drive first:")
        print(f"   net use Z: \\\\DVCIISILON01\\cm_share3 /persistent:yes")
        return 1
    
    # Use the ArchiveContentsComparer for detailed JAR comparison
    print("Starting archive comparison...")
    print()
    
    try:
        comparer = ArchiveContentsComparer(folder1, folder2)
        comparer.deep_nested_archives = True  # Enable deep nested archive inspection
        
        # Compare the two JAR files
        results = comparer.compare_archives()
        
        # Check if comparison was successful
        if results is None or (isinstance(results, list) and len(results) == 0 and not comparer.results):
            print("ERROR: Comparison failed - no results generated")
            print()
            print("Please check:")
            print(f"  - Path 1 exists: {Path(folder1).exists()}")
            print(f"  - Path 2 exists: {Path(folder2).exists()}")
            if Path(folder1).exists():
                print(f"  - Path 1 is file: {Path(folder1).is_file()}")
                print(f"  - Path 1 is readable: {os.access(folder1, os.R_OK)}")
            if Path(folder2).exists():
                print(f"  - Path 2 is file: {Path(folder2).is_file()}")
                print(f"  - Path 2 is readable: {os.access(folder2, os.R_OK)}")
            return 1
        
        # Generate report
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        txt_report = f"crm_sdk_comparison_{timestamp}.txt"
        html_report = f"crm_sdk_comparison_{timestamp}.html"
        
        # Save reports
        comparer.save_archive_report(txt_report)
        
        with open(html_report, 'w', encoding='utf-8') as f:
            f.write(comparer.generate_archive_html_report())
        
        print("Comparison completed!")
        print()
        print(f"Text report saved to: {txt_report}")
        print(f"HTML report saved to: {html_report}")
        print()
        
        # Print summary
        print(comparer.generate_archive_report())
        
        return 0
        
    except Exception as e:
        print(f"ERROR during comparison: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    try:
        sys.exit(compare_crm_sdk_jars())
    except KeyboardInterrupt:
        print("\nComparison interrupted by user.")
        sys.exit(1)

