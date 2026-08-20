#!/usr/bin/env python3
"""
Test script to verify naming convention comparison fix.
This creates two test folders and verifies that exact matches are counted correctly.
"""
import tempfile
import shutil
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import NamingConventionComparer

def test_naming_convention_with_duplicates():
    """Test that duplicate filenames are handled correctly."""
    print("=" * 80)
    print("Testing Naming Convention Comparison with Duplicate Filenames")
    print("=" * 80)
    
    # Create temporary test folders
    temp_dir = Path(tempfile.mkdtemp(prefix="test_naming_"))
    folder1 = temp_dir / "folder1"
    folder2 = temp_dir / "folder2"
    
    try:
        # Create folder structure with duplicate filenames
        # Folder 1
        (folder1 / "lib").mkdir(parents=True, exist_ok=True)
        (folder1 / "modules").mkdir(parents=True, exist_ok=True)
        (folder1 / "test").mkdir(parents=True, exist_ok=True)
        
        (folder1 / "lib" / "common.jar").write_text("lib common")
        (folder1 / "modules" / "common.jar").write_text("modules common")
        (folder1 / "test" / "common.jar").write_text("test common")
        (folder1 / "lib" / "util.jar").write_text("util")
        (folder1 / "README.md").write_text("readme1")
        (folder1 / "lib" / "README.md").write_text("readme1 lib")
        
        # Folder 2 (similar structure)
        (folder2 / "lib").mkdir(parents=True, exist_ok=True)
        (folder2 / "modules").mkdir(parents=True, exist_ok=True)
        (folder2 / "test").mkdir(parents=True, exist_ok=True)
        
        (folder2 / "lib" / "common.jar").write_text("lib common")
        (folder2 / "modules" / "common.jar").write_text("modules common")
        (folder2 / "test" / "common.jar").write_text("test common")
        (folder2 / "lib" / "util.jar").write_text("util")
        (folder2 / "README.md").write_text("readme2")
        (folder2 / "lib" / "README.md").write_text("readme2 lib")
        # Add one extra file in folder2
        (folder2 / "lib" / "extra.jar").write_text("extra")
        
        print(f"\nTest folders created:")
        print(f"  Folder 1: {folder1}")
        print(f"  Folder 2: {folder2}")
        print()
        
        # Run comparison
        comparer = NamingConventionComparer(str(folder1), str(folder2))
        comparer.should_cleanup_temp_folders = False  # Keep temp folders for inspection
        
        print("Running comparison...")
        results = comparer.compare_naming_conventions()
        
        # Analyze results
        exact_matches = [r for r in results if r.is_exact_match]
        missing_from_folder2 = [r for r in results if r.exists_in_folder1 and not r.exists_in_folder2]
        missing_from_folder1 = [r for r in results if r.exists_in_folder2 and not r.exists_in_folder1]
        
        print()
        print("=" * 80)
        print("RESULTS:")
        print("=" * 80)
        print(f"Total unique filenames: {len(results)}")
        print(f"Exact name matches: {len(exact_matches)}")
        print(f"Missing from folder 2: {len(missing_from_folder2)}")
        print(f"Missing from folder 1: {len(missing_from_folder1)}")
        print()
        
        # Expected results:
        # - common.jar: MATCH (in both)
        # - util.jar: MATCH (in both)
        # - README.md: MATCH (in both)
        # - extra.jar: MISSING FROM FOLDER 1
        # Total: 4 unique filenames, 3 exact matches, 0 missing from folder2, 1 missing from folder1
        
        print("Expected:")
        print(f"  Total unique filenames: 4")
        print(f"  Exact name matches: 3 (common.jar, util.jar, README.md)")
        print(f"  Missing from folder 2: 0")
        print(f"  Missing from folder 1: 1 (extra.jar)")
        print()
        
        # Verify
        if len(exact_matches) == 3:
            print("✅ TEST PASSED: Exact matches counted correctly!")
            print()
            print("Details of exact matches:")
            for r in exact_matches:
                print(f"  {r.file_name}: {len(r.relative_paths1)} in folder1, {len(r.relative_paths2)} in folder2")
                if len(r.relative_paths1) > 1 or len(r.relative_paths2) > 1:
                    for p in r.relative_paths1:
                        print(f"    Folder 1: {p}")
                    for p in r.relative_paths2:
                        print(f"    Folder 2: {p}")
            return True
        else:
            print(f"❌ TEST FAILED: Expected 3 exact matches, got {len(exact_matches)}")
            print()
            print("All results:")
            for r in results:
                print(f"  {r.file_name}:")
                print(f"    exists_in_folder1: {r.exists_in_folder1}")
                print(f"    exists_in_folder2: {r.exists_in_folder2}")
                print(f"    is_exact_match: {r.is_exact_match}")
                print(f"    relative_paths1: {r.relative_paths1}")
                print(f"    relative_paths2: {r.relative_paths2}")
            return False
            
    finally:
        # Cleanup
        print()
        print(f"Cleaning up test folders: {temp_dir}")
        try:
            shutil.rmtree(temp_dir)
            print("Cleanup complete.")
        except Exception as e:
            print(f"Cleanup failed: {e}")
            print(f"Please manually delete: {temp_dir}")

if __name__ == "__main__":
    success = test_naming_convention_with_duplicates()
    sys.exit(0 if success else 1)











