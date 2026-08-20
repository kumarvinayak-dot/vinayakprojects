#!/usr/bin/env python3
"""
Standalone Naming Convention Comparison Script (No Web Server Required)
Run this directly to compare your SDK folders.
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from folder_compare import NamingConventionComparer, ArchiveContentsComparer
from datetime import datetime

def main():
    print("=" * 80)
    print("FOLDER COMPARISON - STANDALONE MODE")
    print("=" * 80)
    print()
    
    # Your SDK folder paths
    folder1 = r"\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\AmdocsCRM9.22.PB1.HFB10_release\release\SDK"
    folder2 = r"\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\AmdocsCRM9.22.PB1.HFB11_release\release\SDK"
    
    print("Folder 1 (SDK):")
    print(f"  {folder1}")
    print()
    print("Folder 2 (SDK):")
    print(f"  {folder2}")
    print()
    
    # Ask user what type of comparison
    print("Select comparison type:")
    print("  1 = Naming Convention Comparison")
    print("  2 = Archive Contents Comparison")
    print()
    choice = input("Enter choice (1 or 2) [default: 1]: ").strip() or "1"
    print()
    
    # Ask about local copy
    print("Options:")
    local_copy = input("Use local copy for faster comparison? (y/n) [default: n]: ").strip().lower() or "n"
    use_local = (local_copy == "y")
    print()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    if choice == "2":
        # Archive comparison
        print("=" * 80)
        print("RUNNING ARCHIVE CONTENTS COMPARISON...")
        print("=" * 80)
        print()
        
        comparer = ArchiveContentsComparer(folder1, folder2)
        comparer.use_local_copy = use_local
        comparer.max_retries = 5
        comparer.retry_delay = 1.0
        
        try:
            results = comparer.compare_archives()
            
            # Save reports
            output_filename = f"ascf_archives_report_{timestamp}.txt"
            comparer.save_archive_report(output_filename)
            
            print()
            print("=" * 80)
            print(f"✅ Archive comparison completed!")
            print("=" * 80)
            print(f"  Total archives: {len(results)}")
            print(f"  Text report:  {output_filename}")
            print(f"  HTML report:  {Path(output_filename).with_suffix('.html')}")
            print()
            
        except Exception as e:
            print()
            print("=" * 80)
            print(f"❌ Archive comparison failed: {e}")
            print("=" * 80)
            import traceback
            traceback.print_exc()
    
    else:
        # Naming convention comparison (default)
        print("=" * 80)
        print("RUNNING NAMING CONVENTION COMPARISON...")
        print("=" * 80)
        print()
        
        comparer = NamingConventionComparer(folder1, folder2)
        comparer.similarity_threshold = 0.6
        comparer.use_local_copy = use_local
        comparer.max_retries = 5
        comparer.retry_delay = 1.0
        
        try:
            results = comparer.compare_naming_conventions()
            
            # Save reports
            output_filename = f"ascf_naming_convention_report_{timestamp}.txt"
            comparer.save_naming_convention_report(output_filename)
            
            # Calculate stats
            exact_matches = [r for r in results if r.is_exact_match]
            missing_from_folder2 = [r for r in results if r.exists_in_folder1 and not r.exists_in_folder2]
            missing_from_folder1 = [r for r in results if r.exists_in_folder2 and not r.exists_in_folder1]
            
            print()
            print("=" * 80)
            print(f"✅ Naming convention comparison completed!")
            print("=" * 80)
            print(f"  Total unique filenames: {len(results)}")
            print(f"  Exact name matches:     {len(exact_matches)}")
            print(f"  Missing from folder 2:  {len(missing_from_folder2)}")
            print(f"  Missing from folder 1:  {len(missing_from_folder1)}")
            print()
            print(f"  Text report:  {output_filename}")
            print(f"  HTML report:  {Path(output_filename).with_suffix('.html')}")
            print()
            
            if len(exact_matches) > 0:
                print(f"✅ EXACT MATCHES ARE WORKING! Found {len(exact_matches)} exact matches.")
            else:
                print(f"⚠️ No exact matches found - check the logs above for issues.")
            print()
            
        except Exception as e:
            print()
            print("=" * 80)
            print(f"❌ Naming convention comparison failed: {e}")
            print("=" * 80)
            import traceback
            traceback.print_exc()
    
    print()
    print("You can now open the HTML report in your browser:")
    print(f"  file:///{Path.cwd()}/{Path(output_filename).with_suffix('.html')}")
    print()
    input("Press Enter to exit...")

if __name__ == "__main__":
    main()











