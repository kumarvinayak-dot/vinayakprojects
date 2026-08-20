"""
Fast archive comparison - compares JAR/WAR/EAR names directly from zip without full extraction.
"""
import sys
from pathlib import Path
import zipfile
from collections import defaultdict
import time

def list_archives_in_zip(zip_path, extensions=['.jar', '.war', '.ear', '.zip']):
    """List all archive files inside a zip without extracting."""
    archives = {}
    try:
        print(f"\n📂 Reading {zip_path.name}...")
        start = time.time()
        
        with zipfile.ZipFile(zip_path, 'r') as zf:
            members = zf.namelist()
            total = len(members)
            print(f"   Found {total} total entries")
            
            count = 0
            for member in members:
                # Check if it's an archive
                if any(member.lower().endswith(ext) for ext in extensions):
                    # Get just the filename
                    filename = Path(member).name
                    if filename and not member.endswith('/'):
                        # Store full path for reference
                        if filename in archives:
                            # Handle duplicates - store as list
                            if not isinstance(archives[filename], list):
                                archives[filename] = [archives[filename]]
                            archives[filename].append(member)
                        else:
                            archives[filename] = member
                        count += 1
                        
                        # Show progress every 100 archives found
                        if count % 100 == 0:
                            print(f"   Found {count} archives so far...")
            
            elapsed = time.time() - start
            print(f"   ✅ Found {count} archives in {elapsed:.1f} seconds")
            return archives
            
    except Exception as e:
        print(f"   ❌ Error reading zip: {e}")
        return {}

def compare_archives():
    """Compare archive names between two lightsaber.zip files quickly."""
    
    # The user's paths
    folder1 = Path(r"\\RAAISILON\cmprod_dev\pbg\cmdev\LS\v10.1\Daily\BN343\lightsaber.zip")
    folder2 = Path(r"\\RAAISILON\cmprod_dev\pbg\cmdev\LS\v10.1\Daily\BN344\lightsaber.zip")
    
    print("=" * 100)
    print("FAST ARCHIVE NAME COMPARISON")
    print("=" * 100)
    print(f"Folder 1: {folder1}")
    print(f"Folder 2: {folder2}")
    print("=" * 100)
    print("\n⚡ Using fast method - reading directly from zip without extraction...")
    
    # Check if files exist
    if not folder1.exists():
        print(f"❌ ERROR: Folder 1 does not exist: {folder1}")
        return
    
    if not folder2.exists():
        print(f"❌ ERROR: Folder 2 does not exist: {folder2}")
        return
    
    # List archives in both zips
    print("\n" + "=" * 100)
    print("STEP 1: Reading archives from both builds...")
    print("=" * 100)
    
    archives1 = list_archives_in_zip(folder1)
    archives2 = list_archives_in_zip(folder2)
    
    if not archives1 or not archives2:
        print("\n❌ Failed to read archives from one or both zip files")
        return
    
    # Categorize by type
    print("\n" + "=" * 100)
    print("STEP 2: Categorizing archives...")
    print("=" * 100)
    
    def categorize_archives(archives):
        cats = {
            'jar': {},
            'war': {},
            'ear': {},
            'zip': {},
            'other': {}
        }
        for name, path in archives.items():
            if name.lower().endswith('.jar'):
                cats['jar'][name] = path
            elif name.lower().endswith('.war'):
                cats['war'][name] = path
            elif name.lower().endswith('.ear'):
                cats['ear'][name] = path
            elif name.lower().endswith('.zip'):
                cats['zip'][name] = path
            else:
                cats['other'][name] = path
        return cats
    
    cats1 = categorize_archives(archives1)
    cats2 = categorize_archives(archives2)
    
    print(f"\n📊 BN343 Archives:")
    print(f"   JAR:   {len(cats1['jar'])} files")
    print(f"   WAR:   {len(cats1['war'])} files")
    print(f"   EAR:   {len(cats1['ear'])} files")
    print(f"   ZIP:   {len(cats1['zip'])} files")
    print(f"   Other: {len(cats1['other'])} files")
    print(f"   TOTAL: {len(archives1)} archives")
    
    print(f"\n📊 BN344 Archives:")
    print(f"   JAR:   {len(cats2['jar'])} files")
    print(f"   WAR:   {len(cats2['war'])} files")
    print(f"   EAR:   {len(cats2['ear'])} files")
    print(f"   ZIP:   {len(cats2['zip'])} files")
    print(f"   Other: {len(cats2['other'])} files")
    print(f"   TOTAL: {len(archives2)} archives")
    
    # Compare
    print("\n" + "=" * 100)
    print("STEP 3: Finding differences...")
    print("=" * 100)
    
    # Find differences for each category
    for cat_name in ['jar', 'war', 'ear', 'zip']:
        set1 = set(cats1[cat_name].keys())
        set2 = set(cats2[cat_name].keys())
        
        only_in_1 = sorted(set1 - set2)
        only_in_2 = sorted(set2 - set1)
        in_both = sorted(set1 & set2)
        
        print(f"\n{'='*100}")
        print(f"{cat_name.upper()} FILES")
        print(f"{'='*100}")
        print(f"✅ Exact matches: {len(in_both)}")
        print(f"❌ Only in BN343: {len(only_in_1)}")
        print(f"❌ Only in BN344: {len(only_in_2)}")
        
        if only_in_1:
            print(f"\n📁 Only in BN343 ({len(only_in_1)} files):")
            print("-" * 100)
            for name in only_in_1:
                path = cats1[cat_name][name]
                if isinstance(path, list):
                    print(f"  ❌ {name}")
                    for p in path:
                        print(f"     📍 {p}")
                else:
                    print(f"  ❌ {name}")
                    print(f"     📍 {path}")
        
        if only_in_2:
            print(f"\n📁 Only in BN344 ({len(only_in_2)} files):")
            print("-" * 100)
            for name in only_in_2:
                path = cats2[cat_name][name]
                if isinstance(path, list):
                    print(f"  ✅ {name}")
                    for p in path:
                        print(f"     📍 {p}")
                else:
                    print(f"  ✅ {name}")
                    print(f"     📍 {path}")
    
    # Save to file
    print("\n" + "=" * 100)
    print("STEP 4: Saving report...")
    print("=" * 100)
    
    output_file = "fast_archive_comparison.txt"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("=" * 100 + "\n")
        f.write("FAST ARCHIVE NAME COMPARISON\n")
        f.write("=" * 100 + "\n")
        f.write(f"BN343: {folder1}\n")
        f.write(f"BN344: {folder2}\n")
        f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        for cat_name in ['jar', 'war', 'ear', 'zip']:
            set1 = set(cats1[cat_name].keys())
            set2 = set(cats2[cat_name].keys())
            
            only_in_1 = sorted(set1 - set2)
            only_in_2 = sorted(set2 - set1)
            in_both = sorted(set1 & set2)
            
            f.write("=" * 100 + "\n")
            f.write(f"{cat_name.upper()} FILES\n")
            f.write("=" * 100 + "\n")
            f.write(f"Exact matches: {len(in_both)}\n")
            f.write(f"Only in BN343: {len(only_in_1)}\n")
            f.write(f"Only in BN344: {len(only_in_2)}\n\n")
            
            if only_in_1:
                f.write(f"Only in BN343 ({len(only_in_1)} files):\n")
                f.write("-" * 100 + "\n")
                for name in only_in_1:
                    path = cats1[cat_name][name]
                    if isinstance(path, list):
                        f.write(f"  [X] {name}\n")
                        for p in path:
                            f.write(f"      Path: {p}\n")
                    else:
                        f.write(f"  [X] {name}\n")
                        f.write(f"      Path: {path}\n")
                f.write("\n")
            
            if only_in_2:
                f.write(f"Only in BN344 ({len(only_in_2)} files):\n")
                f.write("-" * 100 + "\n")
                for name in only_in_2:
                    path = cats2[cat_name][name]
                    if isinstance(path, list):
                        f.write(f"  [+] {name}\n")
                        for p in path:
                            f.write(f"      Path: {p}\n")
                    else:
                        f.write(f"  [+] {name}\n")
                        f.write(f"      Path: {path}\n")
                f.write("\n")
            
            if in_both:
                f.write(f"Exact matches ({len(in_both)} files):\n")
                f.write("-" * 100 + "\n")
                for name in in_both[:50]:  # First 50
                    f.write(f"  [OK] {name}\n")
                if len(in_both) > 50:
                    f.write(f"  ... and {len(in_both) - 50} more\n")
                f.write("\n")
    
    print(f"✅ Report saved to: {output_file}")
    
    print("\n" + "=" * 100)
    print("✅ COMPARISON COMPLETE")
    print("=" * 100)

if __name__ == "__main__":
    try:
        start_time = time.time()
        compare_archives()
        elapsed = time.time() - start_time
        print(f"\n⏱️  Total time: {elapsed:.1f} seconds")
    except KeyboardInterrupt:
        print("\n\n⚠️  Comparison interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

