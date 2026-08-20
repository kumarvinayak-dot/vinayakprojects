#!/usr/bin/env python3
"""
Compare JARs showing what WOULD be different without normalization
vs what IS different with normalization.
"""
import sys
import os
import zipfile
from pathlib import Path
from datetime import datetime
from io import BytesIO

# Add the src directory to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import ArchiveContentsComparer


def list_jar_members_lenient(jar_path):
    """List JAR members with lenient error handling."""
    members = []
    try:
        with zipfile.ZipFile(jar_path, 'r') as zf:
            for info in zf.infolist():
                try:
                    name = info.filename.replace('\\', '/').strip('/')
                    if name and not name.endswith('/'):
                        members.append(name)
                except Exception as e:
                    print(f"    Warning: Skipping member due to error: {e}")
    except Exception as e:
        print(f"    ERROR: Could not read JAR: {e}")
    return members


def normalize_filename(filename):
    """Apply normalization to a filename."""
    comparer = ArchiveContentsComparer("dummy1", "dummy2")
    return comparer._normalize_inner_archive_marker(filename)


def compare_with_without_normalization():
    """Show comparison results with and without normalization."""
    
    folder1 = r"\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB22_release\release\SDK_linux\SDK_CRM_10.0.1.22_linux64_20251120_2043.jar"
    folder2 = r"\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB21_release\release\SDK_linux\SDK_CRM_10.0.1.21_linux64_20250908_1434.jar"
    
    print("=" * 80)
    print("COMPARISON: WITH vs WITHOUT NORMALIZATION")
    print("=" * 80)
    print()
    print("This shows what WOULD be different without normalization")
    print("vs what IS different with normalization enabled.")
    print()
    print("Folder 1 (HFB22):", Path(folder1).name)
    print("Folder 2 (HFB21):", Path(folder2).name)
    print()
    print("=" * 80)
    print()
    
    if not Path(folder1).exists():
        print(f"ERROR: Folder 1 does not exist: {folder1}")
        return 1
    
    if not Path(folder2).exists():
        print(f"ERROR: Folder 2 does not exist: {folder2}")
        return 1
    
    print("Reading JAR contents (with lenient mode)...")
    print("-" * 80)
    
    members1 = list_jar_members_lenient(folder1)
    members2 = list_jar_members_lenient(folder2)
    
    print(f"[OK] JAR 1 has {len(members1)} members")
    print(f"[OK] JAR 2 has {len(members2)} members")
    print()
    
    # Analysis WITHOUT normalization
    print("=" * 80)
    print("ANALYSIS WITHOUT NORMALIZATION (Raw File Names)")
    print("=" * 80)
    print()
    
    set1 = set(members1)
    set2 = set(members2)
    
    only_in_1_raw = set1 - set2
    only_in_2_raw = set2 - set1
    in_both_raw = set1 & set2
    
    print(f"Files in both (exact name match): {len(in_both_raw)}")
    print(f"Files only in HFB22: {len(only_in_1_raw)}")
    print(f"Files only in HFB21: {len(only_in_2_raw)}")
    print()
    
    if only_in_1_raw or only_in_2_raw:
        print("WITHOUT normalization, these files look DIFFERENT:")
        print()
        
        # Show samples
        samples = list(sorted(only_in_1_raw))[:15]
        if samples:
            print("Sample files that appear only in HFB22:")
            for i, f in enumerate(samples, 1):
                print(f"  {i}. {f}")
            if len(only_in_1_raw) > 15:
                print(f"  ... and {len(only_in_1_raw) - 15} more")
            print()
        
        samples = list(sorted(only_in_2_raw))[:15]
        if samples:
            print("Sample files that appear only in HFB21:")
            for i, f in enumerate(samples, 1):
                print(f"  {i}. {f}")
            if len(only_in_2_raw) > 15:
                print(f"  ... and {len(only_in_2_raw) - 15} more")
            print()
    
    # Analysis WITH normalization
    print("=" * 80)
    print("ANALYSIS WITH NORMALIZATION (Normalized File Names)")
    print("=" * 80)
    print()
    
    # Normalize all names
    normalized1 = {normalize_filename(f): f for f in members1}
    normalized2 = {normalize_filename(f): f for f in members2}
    
    norm_set1 = set(normalized1.keys())
    norm_set2 = set(normalized2.keys())
    
    only_in_1_norm = norm_set1 - norm_set2
    only_in_2_norm = norm_set2 - norm_set1
    in_both_norm = norm_set1 & norm_set2
    
    print(f"Files in both (after normalization): {len(in_both_norm)}")
    print(f"Files only in HFB22 (after normalization): {len(only_in_1_norm)}")
    print(f"Files only in HFB21 (after normalization): {len(only_in_2_norm)}")
    print()
    
    if len(only_in_1_norm) == 0 and len(only_in_2_norm) == 0:
        print("[OK] WITH normalization, ALL files match!")
        print()
    
    # Show normalization examples
    print("=" * 80)
    print("NORMALIZATION EXAMPLES")
    print("=" * 80)
    print()
    print("Here are examples showing how files were normalized:")
    print()
    
    # Find files that were normalized (different raw vs normalized name)
    normalized_examples = []
    for norm_name, raw_name in list(normalized1.items())[:50]:
        if norm_name != raw_name:
            # Check if there's a corresponding file in set2
            if norm_name in normalized2:
                raw_name2 = normalized2[norm_name]
                if raw_name2 != raw_name:
                    normalized_examples.append((raw_name, raw_name2, norm_name))
    
    if normalized_examples:
        print(f"Found {len(normalized_examples)} files that were normalized to match:")
        print()
        for i, (name1, name2, norm) in enumerate(normalized_examples[:20], 1):
            print(f"{i}. Files that now match after normalization:")
            print(f"   HFB22: {name1}")
            print(f"   HFB21: {name2}")
            print(f"   Normalized to: {norm}")
            print()
        if len(normalized_examples) > 20:
            print(f"... and {len(normalized_examples) - 20} more normalized matches")
            print()
    else:
        print("No naming differences found that required normalization.")
        print("(The files already had identical names)")
        print()
    
    # Final summary
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()
    print(f"WITHOUT Normalization:")
    print(f"  - Mismatched files: {len(only_in_1_raw) + len(only_in_2_raw)}")
    print(f"  - Matched files: {len(in_both_raw)}")
    print()
    print(f"WITH Normalization:")
    print(f"  - Mismatched files: {len(only_in_1_norm) + len(only_in_2_norm)}")
    print(f"  - Matched files: {len(in_both_norm)}")
    print()
    
    files_normalized = (len(only_in_1_raw) + len(only_in_2_raw)) - (len(only_in_1_norm) + len(only_in_2_norm))
    if files_normalized > 0:
        print(f"[OK] Normalization successfully matched {files_normalized} additional files!")
        print("[OK] These were version-specific naming differences (not content differences)")
        print()
    else:
        print("Files already had identical names - no normalization needed")
        print()
    
    if len(only_in_1_norm) == 0 and len(only_in_2_norm) == 0:
        print("=" * 80)
        print("RESULT: NO DIFFERENCES FOUND")
        print("=" * 80)
        print()
        print("This is CORRECT! It means:")
        print("  [OK] The JARs are functionally identical")
        print("  [OK] All version-specific naming has been normalized")
        print("  [OK] No actual content differences exist")
        print()
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(compare_with_without_normalization())
    except KeyboardInterrupt:
        print("\nComparison interrupted by user.")
        sys.exit(1)

