"""
Test script to verify zip file comparison functionality.
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from folder_compare import FolderComparer, NamingConventionComparer

def test_zip_comparison():
    """Test comparing two zip files."""
    
    # The user's paths
    folder1 = r"\\RAAISILON\cmprod\pbg\cmbuild\LS\v1000_PMX102_1\10.0.0.0.0\Daily\BN212\lightsaber.zip"
    folder2 = r"\\RAAISILON\cmprod\pbg\cmbuild\LS\v1000_PMX102_1\10.0.0.0.0\Daily\BN210\lightsaber.zip"
    
    print(f"Comparing:")
    print(f"  Folder 1: {folder1}")
    print(f"  Folder 2: {folder2}")
    print()
    
    # Run naming convention comparison
    print("=" * 80)
    print("NAMING CONVENTION COMPARISON")
    print("=" * 80)
    
    naming_comparer = NamingConventionComparer(folder1, folder2)
    results = naming_comparer.compare_naming_conventions()
    
    if results:
        print(f"\nFound {len(results)} files to compare")
        
        # Generate and print report
        report = naming_comparer.generate_naming_convention_report()
        print(report)
        
        # Save report
        output_file = "naming_convention_report.txt"
        naming_comparer.save_naming_convention_report(output_file)
        print(f"\nReport saved to: {output_file}")
    else:
        print("No results from naming convention comparison")
        if naming_comparer.comparison_error:
            print(f"Error: {naming_comparer.comparison_error}")
    
    # Clean up temp folders from naming comparison
    print("\nCleaning up naming comparison temp folders...")
    naming_comparer.cleanup_temp_folders()
    
    print()
    print("=" * 80)
    print("FULL FOLDER COMPARISON")
    print("=" * 80)
    
    # Create a new comparer for full folder comparison
    comparer = FolderComparer(folder1, folder2)
    
    # Enable deep archive comparison to show nested archives
    comparer.compare_archive_contents = True
    
    # Run full folder comparison (includes content differences)
    results = comparer.compare_folders()
    
    if results:
        print(f"\nFound {len(results)} files to compare")
        
        # Generate and print report
        report = comparer.generate_report()
        print(report)
        
        # Save report
        output_file = "full_comparison_report.txt"
        comparer.save_report(output_file)
        print(f"\nReport saved to: {output_file}")
    else:
        print("No results from full comparison")
        if comparer.comparison_error:
            print(f"Error: {comparer.comparison_error}")
    
    # Clean up temporary folders
    print("\nCleaning up temporary folders...")
    comparer.cleanup_temp_folders()
    print("Done!")

if __name__ == "__main__":
    try:
        test_zip_comparison()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

