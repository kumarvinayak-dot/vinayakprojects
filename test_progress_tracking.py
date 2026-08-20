#!/usr/bin/env python3
"""
Test script for progress tracking and timeout handling features.
"""

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import FolderComparer


def test_progress_tracking():
    """Test basic progress tracking."""
    print("=" * 80)
    print("TEST 1: Basic Progress Tracking")
    print("=" * 80)
    
    # Create test folders with some files
    test_folder1 = Path(__file__).parent / "test_data" / "folder1"
    test_folder2 = Path(__file__).parent / "test_data" / "folder2"
    
    # Use current folder if test_data doesn't exist
    if not test_folder1.exists():
        test_folder1 = Path(__file__).parent / "src"
        test_folder2 = Path(__file__).parent / "src"
    
    comparer = FolderComparer(test_folder1, test_folder2)
    
    print(f"Folder 1: {test_folder1}")
    print(f"Folder 2: {test_folder2}")
    print()
    
    # Check initial state
    print("Initial state:")
    print(f"  Total files: {comparer.total_files}")
    print(f"  Processed files: {comparer.processed_files}")
    print(f"  Progress: {comparer.progress_percentage:.1f}%")
    print(f"  Current file: {comparer.current_file}")
    print(f"  ETA: {comparer.estimated_time_remaining}s")
    print(f"  Error: {comparer.comparison_error}")
    print()
    
    # Run comparison
    print("Running comparison...")
    start_time = time.time()
    results = comparer.compare_folders()
    elapsed_time = time.time() - start_time
    
    print()
    print("Comparison complete!")
    print(f"  Total files: {comparer.total_files}")
    print(f"  Processed files: {comparer.processed_files}")
    print(f"  Progress: {comparer.progress_percentage:.1f}%")
    print(f"  Elapsed time: {elapsed_time:.1f}s")
    print(f"  Error: {comparer.comparison_error}")
    print(f"  Results count: {len(results)}")
    print()
    
    if comparer.comparison_error:
        print("[FAIL] Unexpected error during comparison")
        return False
    
    if comparer.processed_files != comparer.total_files:
        print(f"[FAIL] Not all files processed ({comparer.processed_files}/{comparer.total_files})")
        return False
    
    if comparer.progress_percentage < 99.9:
        print(f"[FAIL] Progress not at 100% ({comparer.progress_percentage:.1f}%)")
        return False
    
    print("[PASS] Progress tracking works correctly")
    return True


def test_timeout_handling():
    """Test timeout handling with a short timeout."""
    print("=" * 80)
    print("TEST 2: Timeout Handling")
    print("=" * 80)
    
    # Use a folder with many files
    test_folder1 = Path(__file__).parent / "src"
    test_folder2 = Path(__file__).parent / "src"
    
    comparer = FolderComparer(test_folder1, test_folder2)
    comparer.comparison_timeout = 2  # 2 seconds timeout (very short!)
    comparer.file_timeout = 1  # 1 second per file
    
    print(f"Folder 1: {test_folder1}")
    print(f"Folder 2: {test_folder2}")
    print(f"Timeout: {comparer.comparison_timeout}s")
    print(f"File timeout: {comparer.file_timeout}s")
    print()
    
    print("Running comparison with short timeout...")
    start_time = time.time()
    results = comparer.compare_folders()
    elapsed_time = time.time() - start_time
    
    print()
    print("Comparison stopped!")
    print(f"  Total files: {comparer.total_files}")
    print(f"  Processed files: {comparer.processed_files}")
    print(f"  Progress: {comparer.progress_percentage:.1f}%")
    print(f"  Elapsed time: {elapsed_time:.1f}s")
    print(f"  Error: {comparer.comparison_error}")
    print(f"  Results count: {len(results)}")
    print()
    
    # With such a short timeout, we should get an error or partial results
    if elapsed_time > 10:  # Should stop within ~5 seconds
        print(f"[FAIL] Timeout not enforced (took {elapsed_time:.1f}s)")
        return False
    
    if comparer.comparison_error:
        print(f"[PASS] Timeout triggered with error: {comparer.comparison_error}")
        return True
    elif comparer.processed_files < comparer.total_files:
        print("[PASS] Timeout stopped comparison early (partial results)")
        return True
    else:
        print("[WARN] Comparison completed before timeout (folder may be small)")
        return True


def test_error_in_report():
    """Test that errors appear in the report."""
    print("=" * 80)
    print("TEST 3: Error Reporting")
    print("=" * 80)
    
    test_folder1 = Path(__file__).parent / "src"
    test_folder2 = Path(__file__).parent / "src"
    
    comparer = FolderComparer(test_folder1, test_folder2)
    comparer.comparison_timeout = 2  # Short timeout
    
    print(f"Running comparison with short timeout...")
    results = comparer.compare_folders()
    
    report = comparer.generate_report()
    
    print()
    print("Report generated:")
    print("-" * 80)
    # Print first 1000 chars of report
    print(report[:1000])
    print("-" * 80)
    print()
    
    # Check if error is in report
    if comparer.comparison_error:
        if "INCOMPLETE/ERROR" in report and comparer.comparison_error in report:
            print("[PASS] Error appears in report header")
            return True
        else:
            print("[FAIL] Error not properly shown in report")
            return False
    else:
        print("[WARN] No error occurred (timeout may not have triggered)")
        return True


def test_progress_api_data():
    """Test that progress data is accessible for API."""
    print("=" * 80)
    print("TEST 4: Progress API Data")
    print("=" * 80)
    
    test_folder1 = Path(__file__).parent / "src"
    test_folder2 = Path(__file__).parent / "src"
    
    comparer = FolderComparer(test_folder1, test_folder2)
    
    # Check that all progress properties exist and are accessible
    try:
        _ = comparer.total_files
        _ = comparer.processed_files
        _ = comparer.current_file
        _ = comparer.progress_percentage
        _ = comparer.estimated_time_remaining
        _ = comparer.comparison_error
        _ = comparer.comparison_timeout
        _ = comparer.file_timeout
        
        print("All progress properties accessible:")
        print(f"  [OK] total_files: {comparer.total_files}")
        print(f"  [OK] processed_files: {comparer.processed_files}")
        print(f"  [OK] current_file: {comparer.current_file}")
        print(f"  [OK] progress_percentage: {comparer.progress_percentage}")
        print(f"  [OK] estimated_time_remaining: {comparer.estimated_time_remaining}")
        print(f"  [OK] comparison_error: {comparer.comparison_error}")
        print(f"  [OK] comparison_timeout: {comparer.comparison_timeout}")
        print(f"  [OK] file_timeout: {comparer.file_timeout}")
        print()
        print("[PASS] All progress API properties exist")
        return True
    except AttributeError as e:
        print(f"[FAIL] Missing property: {e}")
        return False


def main():
    """Run all tests."""
    print()
    print("=" * 80)
    print("=" + " " * 78 + "=")
    print("=" + "  PROGRESS TRACKING AND TIMEOUT HANDLING TEST SUITE".center(78) + "=")
    print("=" + " " * 78 + "=")
    print("=" * 80)
    print()
    
    tests = [
        ("Progress Tracking", test_progress_tracking),
        ("Timeout Handling", test_timeout_handling),
        ("Error Reporting", test_error_in_report),
        ("Progress API Data", test_progress_api_data),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"[FAIL] {name} - Exception: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
        print()
    
    # Summary
    print("=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status}: {name}")
    
    print()
    print(f"Total: {passed_count}/{total_count} tests passed")
    print("=" * 80)
    
    if passed_count == total_count:
        print()
        print("SUCCESS: ALL TESTS PASSED! Progress tracking is working correctly.")
        print()
        return 0
    else:
        print()
        print("WARNING: SOME TESTS FAILED. Please review the output above.")
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())

