#!/usr/bin/env python3
"""
Test script to verify progress tracking and ETA calculation work properly.
"""

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import ArchiveContentsComparer, NamingConventionComparer, JarManifestComparer


def test_archive_comparer_progress():
    """Test that ArchiveContentsComparer updates progress correctly."""
    print("=" * 80)
    print("TEST 1: ArchiveContentsComparer Progress Tracking")
    print("=" * 80)
    
    # Use current directory for testing
    test_folder = Path(__file__).parent / "src"
    
    comparer = ArchiveContentsComparer(str(test_folder), str(test_folder))
    
    print(f"Testing with folder: {test_folder}")
    print()
    print("Initial state:")
    print(f"  Total files: {comparer.total_files}")
    print(f"  Processed files: {comparer.processed_files}")
    print(f"  Progress: {comparer.progress_percentage:.1f}%")
    print(f"  ETA: {comparer.estimated_time_remaining}s")
    print()
    
    # Check that _update_progress method exists
    assert hasattr(comparer, '_update_progress'), "Missing _update_progress method"
    
    # Simulate progress
    comparer.total_files = 10
    comparer.processed_files = 0
    comparer.comparison_start_time = time.time()
    
    print("Simulating progress...")
    for i in range(10):
        comparer.processed_files = i + 1
        comparer.current_file = f"file_{i}.zip"
        comparer._update_progress(f"file_{i}.zip")
        time.sleep(0.1)  # Simulate work
        
        print(f"  Progress: {comparer.processed_files}/{comparer.total_files} "
              f"({comparer.progress_percentage:.1f}%) - "
              f"Current: {comparer.current_file} - "
              f"ETA: {comparer.estimated_time_remaining}s")
    
    print()
    if comparer.progress_percentage == 100.0:
        print("[PASS] ArchiveContentsComparer progress tracking works!")
        return True
    else:
        print(f"[FAIL] Progress didn't reach 100% (got {comparer.progress_percentage}%)")
        return False


def test_naming_comparer_progress():
    """Test that NamingConventionComparer has progress tracking."""
    print()
    print("=" * 80)
    print("TEST 2: NamingConventionComparer Progress Tracking")
    print("=" * 80)
    
    test_folder = Path(__file__).parent / "src"
    
    comparer = NamingConventionComparer(str(test_folder), str(test_folder))
    
    # Check that _update_progress method exists
    assert hasattr(comparer, '_update_progress'), "Missing _update_progress method"
    
    # Test manual progress update
    comparer.total_files = 5
    comparer.processed_files = 3
    comparer.comparison_start_time = time.time() - 10  # Started 10 seconds ago
    comparer._update_progress("test_file.jar")
    
    print(f"Progress: {comparer.processed_files}/{comparer.total_files} "
          f"({comparer.progress_percentage:.1f}%) - "
          f"ETA: {comparer.estimated_time_remaining}s")
    
    if comparer.progress_percentage == 60.0:
        print("[PASS] NamingConventionComparer progress tracking works!")
        return True
    else:
        print(f"[FAIL] Progress calculation wrong (expected 60%, got {comparer.progress_percentage}%)")
        return False


def test_jar_manifest_comparer_progress():
    """Test that JarManifestComparer has progress tracking."""
    print()
    print("=" * 80)
    print("TEST 3: JarManifestComparer Progress Tracking")
    print("=" * 80)
    
    test_folder = Path(__file__).parent / "src"
    
    comparer = JarManifestComparer(str(test_folder), str(test_folder), "test.jar")
    
    # Check that _update_progress method exists
    assert hasattr(comparer, '_update_progress'), "Missing _update_progress method"
    
    # Test manual progress update
    comparer.total_files = 1
    comparer.processed_files = 0
    comparer.comparison_start_time = time.time()
    comparer._update_progress("test.jar")
    
    print(f"Progress: {comparer.processed_files}/{comparer.total_files} "
          f"({comparer.progress_percentage:.1f}%)")
    
    # Simulate completion
    time.sleep(0.5)
    comparer.processed_files = 1
    comparer._update_progress("test.jar")
    
    print(f"Progress: {comparer.processed_files}/{comparer.total_files} "
          f"({comparer.progress_percentage:.1f}%)")
    
    if comparer.progress_percentage == 100.0:
        print("[PASS] JarManifestComparer progress tracking works!")
        return True
    else:
        print(f"[FAIL] Progress didn't reach 100% (got {comparer.progress_percentage}%)")
        return False


def main():
    """Run all tests."""
    print()
    print("*" * 80)
    print("*" + " PROGRESS TRACKING AND ETA FIX VERIFICATION ".center(78) + "*")
    print("*" * 80)
    print()
    
    tests = [
        test_archive_comparer_progress,
        test_naming_comparer_progress,
        test_jar_manifest_comparer_progress,
    ]
    
    results = []
    for test_func in tests:
        try:
            passed = test_func()
            results.append(passed)
        except Exception as e:
            print(f"[FAIL] {test_func.__name__} - Exception: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)
    
    # Summary
    print()
    print("=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    passed_count = sum(results)
    total_count = len(results)
    
    print(f"Passed: {passed_count}/{total_count}")
    print()
    
    if passed_count == total_count:
        print("[SUCCESS] All progress tracking tests passed!")
        print()
        print("The ETA calculation should now work properly when comparing")
        print("lightsaber.zip files (or any archives).")
        print()
        print("Progress bar will show:")
        print("  - Actual progress percentage")
        print("  - Current file being processed")
        print("  - Estimated time remaining")
        print()
        return 0
    else:
        print("[FAILURE] Some tests failed. Please review the output above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

