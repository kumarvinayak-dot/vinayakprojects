#!/usr/bin/env python3
import sys
import os
import tempfile
import shutil
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent / "src"  
sys.path.insert(0, str(src_path))

from folder_compare import FolderComparer

def quick_test():
    print("ASCF Folder Comparison - Quick Test")
    print("="*50)
    
    # Create two simple test folders
    base_dir = Path(tempfile.mkdtemp(prefix="test_"))
    folder1 = base_dir / "folder1" 
    folder2 = base_dir / "folder2"
    
    folder1.mkdir()
    folder2.mkdir()
    
    # Create test files
    (folder1 / "file1.txt").write_text("Hello World")
    (folder2 / "file1.txt").write_text("Hello World")  # Same
    (folder1 / "file2.txt").write_text("Version 1")  
    (folder2 / "file2.txt").write_text("Version 2")   # Different
    (folder1 / "only_in_1.txt").write_text("Only in folder 1")
    (folder2 / "only_in_2.txt").write_text("Only in folder 2")
    
    try:
        comparer = FolderComparer(str(folder1), str(folder2))
        results = comparer.compare_folders()
        print(comparer.generate_report())
    finally:
        shutil.rmtree(base_dir)
    
    print("Test completed!")

if __name__ == "__main__":
    quick_test()
