"""
Compare naming conventions of archives (JAR/WAR/EAR/ZIP) inside lightsaber.zip files.
This script focuses on comparing archive file names and nested archives.
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from folder_compare import NamingConventionComparer

def compare_archives():
    """Compare archive naming conventions between two lightsaber.zip files."""
    
    # The user's new paths
    folder1 = r"\\RAAISILON\cmprod_dev\pbg\cmdev\LS\v10.1\Daily\BN343\lightsaber.zip"
    folder2 = r"\\RAAISILON\cmprod_dev\pbg\cmdev\LS\v10.1\Daily\BN344\lightsaber.zip"
    
    print("=" * 100)
    print("ARCHIVE NAMING CONVENTION COMPARISON")
    print("=" * 100)
    print(f"Folder 1: {folder1}")
    print(f"Folder 2: {folder2}")
    print("=" * 100)
    print()
    
    # Create comparer
    print("Initializing comparison...")
    naming_comparer = NamingConventionComparer(folder1, folder2)
    
    # Run comparison
    print("Starting comparison (this may take several minutes for large archives)...")
    results = naming_comparer.compare_naming_conventions()
    
    if not results:
        print("\nNo results from naming convention comparison")
        if naming_comparer.comparison_error:
            print(f"Error: {naming_comparer.comparison_error}")
        # Clean up
        naming_comparer.cleanup_temp_folders()
        return
    
    print(f"\nComparison completed! Found {len(results)} files to compare")
    print()
    
    # Filter to show only archive files (JAR, WAR, EAR, ZIP)
    archive_extensions = {'.jar', '.war', '.ear', '.zip', '.rar', '.tar', '.gz'}
    archive_results = [r for r in results if any(r.file_name.lower().endswith(ext) for ext in archive_extensions)]
    
    print(f"Archive files (JAR/WAR/EAR/ZIP) found: {len(archive_results)}")
    print()
    
    # Generate and save full report
    output_file = "archive_naming_report.txt"
    try:
        report = naming_comparer.generate_naming_convention_report()
        
        # Save to file (UTF-8 encoding)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"Full report saved to: {output_file}")
        
        # Save HTML report
        naming_comparer.save_naming_convention_report(output_file)
        print(f"HTML report saved to: {Path(output_file).with_suffix('.html')}")
    except Exception as e:
        print(f"Error generating report: {e}")
    
    print()
    print("=" * 100)
    print("SUMMARY OF ARCHIVE FILES")
    print("=" * 100)
    
    # Categorize results
    jar_results = [r for r in results if r.file_name.lower().endswith('.jar')]
    war_results = [r for r in results if r.file_name.lower().endswith('.war')]
    ear_results = [r for r in results if r.file_name.lower().endswith('.ear')]
    zip_results = [r for r in results if r.file_name.lower().endswith('.zip')]
    other_archive_results = [r for r in archive_results if r not in jar_results + war_results + ear_results + zip_results]
    
    print(f"\nTotal JAR files: {len(jar_results)}")
    print(f"Total WAR files: {len(war_results)}")
    print(f"Total EAR files: {len(ear_results)}")
    print(f"Total ZIP files: {len(zip_results)}")
    print(f"Other archives: {len(other_archive_results)}")
    print()
    
    # Show differences in JAR files
    jar_diff = [r for r in jar_results if not r.is_exact_match]
    if jar_diff:
        print(f"JAR files with naming differences: {len(jar_diff)}")
        print("-" * 100)
        for result in jar_diff[:20]:  # Show first 20
            if result.exists_in_folder1 and not result.exists_in_folder2:
                print(f"  [MISSING IN BN344] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
            elif result.exists_in_folder2 and not result.exists_in_folder1:
                print(f"  [MISSING IN BN343] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
        if len(jar_diff) > 20:
            print(f"  ... and {len(jar_diff) - 20} more (see full report)")
    else:
        print("[OK] All JAR files have matching names")
    
    print()
    
    # Show differences in WAR files
    war_diff = [r for r in war_results if not r.is_exact_match]
    if war_diff:
        print(f"WAR files with naming differences: {len(war_diff)}")
        print("-" * 100)
        for result in war_diff:
            if result.exists_in_folder1 and not result.exists_in_folder2:
                print(f"  [MISSING IN BN344] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
            elif result.exists_in_folder2 and not result.exists_in_folder1:
                print(f"  [MISSING IN BN343] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
    else:
        print("[OK] All WAR files have matching names")
    
    print()
    
    # Show differences in EAR files
    ear_diff = [r for r in ear_results if not r.is_exact_match]
    if ear_diff:
        print(f"EAR files with naming differences: {len(ear_diff)}")
        print("-" * 100)
        for result in ear_diff:
            if result.exists_in_folder1 and not result.exists_in_folder2:
                print(f"  [MISSING IN BN344] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
            elif result.exists_in_folder2 and not result.exists_in_folder1:
                print(f"  [MISSING IN BN343] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
    else:
        print("[OK] All EAR files have matching names")
    
    print()
    
    # Show differences in other ZIP files
    zip_diff = [r for r in zip_results if not r.is_exact_match]
    if zip_diff:
        print(f"ZIP files with naming differences: {len(zip_diff)}")
        print("-" * 100)
        for result in zip_diff:
            if result.exists_in_folder1 and not result.exists_in_folder2:
                print(f"  [MISSING IN BN344] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
            elif result.exists_in_folder2 and not result.exists_in_folder1:
                print(f"  [MISSING IN BN343] {result.file_name}")
                if result.potential_matches:
                    print(f"    Possible match: {result.potential_matches[0]} (similarity: {result.similarity_scores[0]:.2f})")
    else:
        print("[OK] All ZIP files have matching names")
    
    print()
    print("=" * 100)
    print(f"COMPARISON COMPLETE - Check '{output_file}' for full details")
    print("=" * 100)
    
    # Clean up temporary folders
    print("\nCleaning up temporary extraction folders...")
    naming_comparer.cleanup_temp_folders()
    print("Done!")

if __name__ == "__main__":
    try:
        compare_archives()
    except KeyboardInterrupt:
        print("\n\nComparison interrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()

