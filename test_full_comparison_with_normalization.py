#!/usr/bin/env python3
"""
Test script to demonstrate that full content comparison now includes 
archive normalization logic for version-specific naming patterns.
"""
import sys
from pathlib import Path
from datetime import datetime

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import FolderComparer


def test_full_comparison():
    """Test full content comparison with archive normalization."""
    
    folder1 = r"\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB22_release\release\SDK_linux\SDK_CRM_10.0.1.22_linux64_20251120_2043.jar"
    folder2 = r"\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB21_release\release\SDK_linux\SDK_CRM_10.0.1.21_linux64_20250908_1434.jar"
    
    print("=" * 80)
    print("FULL CONTENT COMPARISON WITH INTEGRATED ARCHIVE NORMALIZATION")
    print("=" * 80)
    print()
    print("This test demonstrates that the FolderComparer now includes:")
    print("  1. Deep archive contents comparison")
    print("  2. Version-specific filename normalization")
    print("  3. .pack.gz file normalization")
    print("  4. All in a single comprehensive comparison")
    print()
    print("=" * 80)
    print()
    
    if not Path(folder1).exists():
        print(f"ERROR: Path 1 not accessible: {folder1}")
        return 1
    
    if not Path(folder2).exists():
        print(f"ERROR: Path 2 not accessible: {folder2}")
        return 1
    
    print("Starting full content comparison...")
    print()
    
    # Create comparer with all features enabled
    comparer = FolderComparer(folder1, folder2)
    comparer.compare_archive_contents = True  # Enable deep archive comparison
    comparer.show_missing_only = False  # Show all differences
    comparer.suppress_warnings = False  # Show all warnings
    
    # Run comparison
    results = comparer.compare_folders()
    
    if not results:
        print("ERROR: No results generated")
        return 1
    
    # Generate reports
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_report = f"full_comparison_with_normalization_{timestamp}.txt"
    html_report = f"full_comparison_with_normalization_{timestamp}.html"
    
    print("=" * 80)
    print("GENERATING REPORTS")
    print("=" * 80)
    print()
    
    # Save text report
    comparer.save_report(txt_report)
    print(f"Text report saved: {txt_report}")
    
    # Save HTML report
    html_content = comparer.generate_html_report()
    with open(html_report, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f"HTML report saved: {html_report}")
    print()
    
    # Display summary
    print("=" * 80)
    print("COMPARISON SUMMARY")
    print("=" * 80)
    print()
    print(comparer.generate_report())
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(test_full_comparison())
    except KeyboardInterrupt:
        print("\nTest interrupted.")
        sys.exit(1)

