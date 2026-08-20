#!/usr/bin/env python3
"""
Test script to verify ArchiveContentsComparer has progress tracking properties.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import ArchiveContentsComparer, NamingConventionComparer, JarManifestComparer


def test_comparer_properties():
    """Test that all comparer classes have progress tracking properties."""
    print("=" * 80)
    print("TESTING PROGRESS TRACKING PROPERTIES ON ALL COMPARERS")
    print("=" * 80)
    print()
    
    # Test ArchiveContentsComparer
    print("1. Testing ArchiveContentsComparer...")
    try:
        comparer1 = ArchiveContentsComparer("test1", "test2")
        assert hasattr(comparer1, 'total_files'), "Missing total_files"
        assert hasattr(comparer1, 'processed_files'), "Missing processed_files"
        assert hasattr(comparer1, 'current_file'), "Missing current_file"
        assert hasattr(comparer1, 'progress_percentage'), "Missing progress_percentage"
        assert hasattr(comparer1, 'estimated_time_remaining'), "Missing estimated_time_remaining"
        assert hasattr(comparer1, 'comparison_start_time'), "Missing comparison_start_time"
        assert hasattr(comparer1, 'comparison_error'), "Missing comparison_error"
        print("   [PASS] ArchiveContentsComparer has all progress tracking properties")
    except AssertionError as e:
        print(f"   [FAIL] ArchiveContentsComparer: {e}")
        return False
    except Exception as e:
        print(f"   [FAIL] ArchiveContentsComparer: {e}")
        return False
    
    # Test NamingConventionComparer
    print("2. Testing NamingConventionComparer...")
    try:
        comparer2 = NamingConventionComparer("test1", "test2")
        assert hasattr(comparer2, 'total_files'), "Missing total_files"
        assert hasattr(comparer2, 'processed_files'), "Missing processed_files"
        assert hasattr(comparer2, 'current_file'), "Missing current_file"
        assert hasattr(comparer2, 'progress_percentage'), "Missing progress_percentage"
        assert hasattr(comparer2, 'estimated_time_remaining'), "Missing estimated_time_remaining"
        assert hasattr(comparer2, 'comparison_start_time'), "Missing comparison_start_time"
        assert hasattr(comparer2, 'comparison_error'), "Missing comparison_error"
        print("   [PASS] NamingConventionComparer has all progress tracking properties")
    except AssertionError as e:
        print(f"   [FAIL] NamingConventionComparer: {e}")
        return False
    except Exception as e:
        print(f"   [FAIL] NamingConventionComparer: {e}")
        return False
    
    # Test JarManifestComparer
    print("3. Testing JarManifestComparer...")
    try:
        comparer3 = JarManifestComparer("test1", "test2", "test.jar")
        assert hasattr(comparer3, 'total_files'), "Missing total_files"
        assert hasattr(comparer3, 'processed_files'), "Missing processed_files"
        assert hasattr(comparer3, 'current_file'), "Missing current_file"
        assert hasattr(comparer3, 'progress_percentage'), "Missing progress_percentage"
        assert hasattr(comparer3, 'estimated_time_remaining'), "Missing estimated_time_remaining"
        assert hasattr(comparer3, 'comparison_start_time'), "Missing comparison_start_time"
        assert hasattr(comparer3, 'comparison_error'), "Missing comparison_error"
        print("   [PASS] JarManifestComparer has all progress tracking properties")
    except AssertionError as e:
        print(f"   [FAIL] JarManifestComparer: {e}")
        return False
    except Exception as e:
        print(f"   [FAIL] JarManifestComparer: {e}")
        return False
    
    print()
    print("=" * 80)
    print("[SUCCESS] All comparer classes have progress tracking properties!")
    print("=" * 80)
    print()
    print("The AttributeError should now be fixed.")
    print("You can now run your JAR comparison without errors.")
    return True


if __name__ == "__main__":
    success = test_comparer_properties()
    sys.exit(0 if success else 1)

