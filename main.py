#!/usr/bin/env python3
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime
import shutil

# Add the src directory to the Python path
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from folder_compare import compare_ascf_patches, FolderComparer, NamingConventionComparer, SimpleFileNameComparer

def prompt_copy_and_compare():
    """Interactively copy two folders from a shared location to
    C:\\Users\\SVINAYAK\\CompareASCFHFBundleReleases, then compare them.
    """
    dest_base = Path(r"C:\\Users\\SVINAYAK\\CompareASCFHFBundleReleases")

    print("Copy and Compare Mode (interactive)")
    print("-")
    folder1_name = input("Enter Folder 1 name to copy: ").strip().strip('"')
    folder2_name = input("Enter Folder 2 name to copy: ").strip().strip('"')
    shared_base1 = input("Enter shared base path for Folder 1 (e.g. \\server\\share or drive path): ").strip().strip('"')
    shared_base2 = input("Enter shared base path for Folder 2 [press Enter to use same as Folder 1]: ").strip().strip('"')

    if not folder1_name or not folder2_name or not shared_base1:
        print("All inputs are required. Aborting.")
        return 1

    shared_base_path1 = Path(shared_base1)
    shared_base_path2 = Path(shared_base2) if shared_base2 else shared_base_path1
    src1 = shared_base_path1 / folder1_name
    src2 = shared_base_path2 / folder2_name

    if not src1.exists():
        print(f"Folder 1 does not exist at shared location: {src1}")
        return 1
    if not src2.exists():
        print(f"Folder 2 does not exist at shared location: {src2}")
        return 1

    # Ensure destination base exists empty
    try:
        if dest_base.exists():
            print(f"Emptying destination folder: {dest_base}")
            shutil.rmtree(dest_base)
        dest_base.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Failed to prepare destination folder {dest_base}: {e}")
        return 1

    dst1 = dest_base / folder1_name
    dst2 = dest_base / folder2_name

    # Copy folders
    try:
        print(f"Copying Folder 1: {src1} -> {dst1}")
        shutil.copytree(src1, dst1)
        print(f"Copying Folder 2: {src2} -> {dst2}")
        shutil.copytree(src2, dst2)
    except Exception as e:
        print(f"Copy failed: {e}")
        return 1

    # Compare using existing logic
    print()
    print("Running comparison on copied folders...")
    comparer = FolderComparer(str(dst1), str(dst2))
    # Keep default behaviors: show_missing_only=True, no local copy needed
    results = comparer.compare_folders()

    if results:
        print()
        print(comparer.generate_report())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_filename = f"ascf_patch_comparison_{timestamp}.txt"
        comparer.save_report(report_filename)
        print(f"Report saved to: {report_filename}")

        # Optional: Copy Folder 2 to server location after report generation
        try:
            print()
            copy_choice = input("Do you want to copy Folder 2 to a server location? (y/N): ").strip().lower()
            if copy_choice == 'y':
                default_server_base = r"\\corp.amdocs.com\shares\St Louis\STLFAS2A Shares\nt2\AUIF_Website"
                dest_base_input = input(
                    f"Enter destination base path for Folder 2 [default: {default_server_base}]: "
                ).strip().strip('"')
                dest_base = Path(dest_base_input) if dest_base_input else Path(default_server_base)

                # Ensure destination base exists
                try:
                    dest_base.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    print(f"Failed to create destination base '{dest_base}': {e}")
                    return 1

                folder2_name = Path(dst2).name
                final_dest = dest_base / folder2_name

                # If target exists, replace it to ensure fresh copy
                try:
                    if final_dest.exists():
                        print(f"Destination already exists. Replacing: {final_dest}")
                        shutil.rmtree(final_dest)
                except Exception as e:
                    print(f"Failed to prepare destination '{final_dest}': {e}")
                    return 1

                try:
                    print(f"Copying Folder 2 to: {final_dest}")
                    shutil.copytree(dst2, final_dest)
                    print("Folder 2 copy completed successfully.")
                except Exception as e:
                    print(f"Copy of Folder 2 failed: {e}")
                    return 1

        except KeyboardInterrupt:
            print("Copy prompt interrupted.")
        except Exception as e:
            print(f"Unexpected error during post-report copy step: {e}")
        return 0
    else:
        print("Comparison failed. Check the copied folders and try again.")
        return 1

def check_network_access(base_path):
    """Check if the network path is accessible and provide specific guidance."""
    import socket
    import subprocess
    
    print(f"Checking network access to: {base_path}")
    
    # Extract server name from UNC path
    if base_path.startswith('\\\\'):
        server_name = base_path.split('\\')[2]
        print(f"Server: {server_name}")
        
        # Test basic connectivity
        try:
            socket.setdefaulttimeout(5)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((server_name, 445))  # SMB port
            sock.close()
            
            if result == 0:
                print("✓ Network connectivity to server: OK")
            else:
                print("✗ Cannot connect to server on SMB port 445")
                print("  This might be a firewall or network issue")
                return False
        except Exception as e:
            print(f"✗ Network connectivity test failed: {e}")
            return False
        
        # Test if path exists (this will show the actual access error)
        test_path = Path(base_path)
        try:
            if test_path.exists():
                print("✓ Network path exists and is accessible")
                
                # Test file-level access by trying to list contents
                try:
                    contents = list(test_path.iterdir())
                    print(f"✓ Can list directory contents ({len(contents)} items found)")
                    
                    # Try to access a subdirectory if it exists
                    test_folders = [f for f in contents if f.is_dir()]
                    if test_folders:
                        test_folder = test_folders[0]
                        print(f"Testing file access in: {test_folder.name}")
                        try:
                            # Try to list files in subdirectory
                            sub_contents = list(test_folder.iterdir())
                            print(f"✓ Can list subdirectory contents ({len(sub_contents)} items)")
                            
                            # Try to read a file if one exists
                            test_files = [f for f in sub_contents if f.is_file()]
                            if test_files:
                                test_file = test_files[0]
                                try:
                                    with open(test_file, 'rb') as f:
                                        f.read(1)  # Try to read just 1 byte
                                    print("✓ Can read file contents")
                                except Exception as e:
                                    print(f"✗ Cannot read file contents: {e}")
                                    print("  You have folder listing permissions but not file read permissions")
                                    return False
                            else:
                                print("  No files found to test read permissions")
                        except Exception as e:
                            print(f"✗ Cannot access subdirectory contents: {e}")
                            return False
                    else:
                        print("  No subdirectories found to test")
                    
                    return True
                except Exception as e:
                    print(f"✗ Cannot list directory contents: {e}")
                    return False
            else:
                print("✗ Network path does not exist")
                return False
        except PermissionError:
            print("✗ Access denied to network path (Windows Error 5)")
            print("  This is an authentication/permission issue")
            return False
        except Exception as e:
            print(f"✗ Error accessing network path: {e}")
            return False
    else:
        # Local path
        test_path = Path(base_path)
        if test_path.exists():
            print("✓ Local path exists and is accessible")
            return True
        else:
            print("✗ Local path does not exist")
            return False

def main():
    parser = argparse.ArgumentParser(description="Compare ASCF patch folders")
    parser.add_argument("--base-path", default=r"\\svinayakmv2\share", help="Base path")
    parser.add_argument("--folder1", default="ASCF_HF_10240pb01hf04", help="First folder")
    parser.add_argument("--folder2", default="ASCF_HF_10240pb01hf05", help="Second folder") 
    parser.add_argument("--custom-folders", action="store_true", help="Use custom folder paths")
    parser.add_argument("--output", help="Output file for report")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum number of retry attempts for network errors (default: 3)")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Initial delay between retries in seconds (default: 1.0)")
    parser.add_argument("--use-local-copy", action="store_true", help="Copy network folders to local temporary directories before comparison")
    parser.add_argument("--no-local-copy", action="store_true", help="Work directly with network paths without copying locally (default: enabled)")
    parser.add_argument("--keep-temp-folders", action="store_true", help="Keep temporary folders after comparison (useful for debugging)")
    parser.add_argument("--diagnose", action="store_true", help="Run network diagnostics to check access to the base path")
    parser.add_argument("--naming-convention", action="store_true", help="Compare only naming conventions of jars and files (no content comparison)")
    parser.add_argument("--simple-names", action="store_true", help="Compare only file names - show differences without any file info comparison")
    parser.add_argument("--similarity-threshold", type=float, default=0.6, help="Similarity threshold for potential name matches (0.0-1.0, default: 0.6)")
    parser.add_argument("--ignore-timestamp-only", action="store_true", help="Ignore files that differ only in modification timestamps when content comparison fails (useful for network/permission issues with JAR files)")
    parser.add_argument("--show-missing-only", action="store_true", default=True, help="Show only files missing in folder 2, do not show different files (default: enabled)")
    parser.add_argument("--show-all-differences", action="store_true", help="Show all differences including content differences (overrides --show-missing-only)")
    parser.add_argument("--exclude-missing-folder2", action="store_true", help="Exclude files missing in folder 2 from the comparison report entirely")
    parser.add_argument("--rename-folder2-only", action="store_true", help="Rename files that exist only in folder 2 to different file names")
    parser.add_argument("--rename-suffix", default="_renamed", help="Suffix to add when renaming files in folder 2 (default: '_renamed')")
    parser.add_argument("--prompt-copy-compare", action="store_true", help="Prompt for two folders, copy them to local CompareASCFHFBundleReleases, then compare")
    parser.add_argument("--no-deep-archive-compare", action="store_true", help="Disable deep archive contents comparison (faster, only compares archive files at file level)")
    parser.add_argument("--deep-compare-if-different", action="store_true", help="Only do deep archive comparison if archive files differ at file level (faster for mostly identical archives)")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress logging during comparison")
    
    args = parser.parse_args()
    
    # Handle conflicting options
    # Default behavior: work directly with network paths (no local copy)
    # --use-local-copy enables local copy feature
    # --no-local-copy explicitly disables local copy (overrides --use-local-copy if both specified)
    if args.no_local_copy:
        use_local_copy = False
    elif args.use_local_copy:
        use_local_copy = True
    else:
        # Default behavior: no local copy (direct network access)
        use_local_copy = False
    
    # Handle report display options
    # Default: show only missing files
    # --show-all-differences overrides default and shows all differences
    show_missing_only = not args.show_all_differences
    
    print("=" * 60)
    print("ASCF PATCH FOLDER COMPARISON TOOL")  
    print("=" * 60)
    
    # Handle diagnosis mode
    if args.diagnose:
        print("NETWORK DIAGNOSTICS MODE")
        print("=" * 30)
        access_ok = check_network_access(args.base_path)
        if not access_ok:
            print()
            print("SOLUTIONS TO TRY:")
            print("1. Run the network setup helper:")
            print("   python setup_network_access.py")
            print()
            print("2. Manual network drive mapping:")
            print("   net use Z: \"{0}\" /persistent:yes".format(args.base_path))
            print("   net use Z: \"{0}\" /user:DOMAIN\\username".format(args.base_path))
            print()
            print("3. Use Windows Credential Manager to store credentials")
            print("4. Contact IT support for network access")
        return 0 if access_ok else 1
    
    if args.prompt_copy_compare:
        return prompt_copy_and_compare()

    if use_local_copy:
        print("Local copy mode: ENABLED (network folders will be copied locally)")
    else:
        print("Direct network access mode: ENABLED (working directly with network paths)")
    
    if args.custom_folders:
        folder1_path = args.folder1
        folder2_path = args.folder2
        
        print("Comparing custom folders:")
        print(f"  Folder 1: {folder1_path}")
        print(f"  Folder 2: {folder2_path}")
        
        if args.simple_names:
            print("Mode: Simple File Name Comparison")
            print()
            
            simple_comparer = SimpleFileNameComparer(folder1_path, folder2_path)
            results = simple_comparer.compare_file_names()
            
            if results:
                print(simple_comparer.generate_simple_report())
                if args.output:
                    simple_comparer.save_simple_report(args.output)
            else:
                print("Simple file name comparison failed. Check folder paths.")
        elif args.naming_convention:
            print("Mode: Naming Convention Comparison")
            print(f"Similarity threshold: {args.similarity_threshold}")
            print()
            
            naming_comparer = NamingConventionComparer(folder1_path, folder2_path)
            naming_comparer.similarity_threshold = args.similarity_threshold
            results = naming_comparer.compare_naming_conventions()
            
            if results:
                print(naming_comparer.generate_naming_convention_report())
                if args.output:
                    naming_comparer.save_naming_convention_report(args.output)
            else:
                print("Naming convention comparison failed. Check folder paths.")
        else:
            print("Mode: Full Content Comparison")
            print()
            
            comparer = FolderComparer(folder1_path, folder2_path)
            comparer.max_retries = args.max_retries
            comparer.retry_delay = args.retry_delay
            comparer.use_local_copy = use_local_copy
            comparer.should_cleanup_temp_folders = not args.keep_temp_folders
            comparer.ignore_timestamp_only_differences = args.ignore_timestamp_only
            comparer.show_missing_only = show_missing_only
            comparer.exclude_missing_from_folder2 = args.exclude_missing_folder2
            comparer.rename_folder2_only_files = args.rename_folder2_only
            comparer.folder2_rename_suffix = args.rename_suffix
            # Performance options for archive comparison
            comparer.compare_archive_contents = not args.no_deep_archive_compare
            comparer.compare_archive_contents_only_if_different = args.deep_compare_if_different
            comparer.show_progress = not args.no_progress
            results = comparer.compare_folders()
            
            if results:
                print()
                print(comparer.generate_report())
                if args.output:
                    comparer.save_report(args.output)
            else:
                print("Comparison failed. Check folder paths.")
    else:
        try:
            if args.simple_names:
                # Use simple file name comparison for ASCF patches
                folder1_name = "ASCF_HF_10240pb01hf04"
                folder2_name = "ASCF_HF_10240pb01hf05"
                
                folder1_path = os.path.join(args.base_path, folder1_name)
                folder2_path = os.path.join(args.base_path, folder2_name)
                
                print(f"Comparing ASCF patch folders (Simple File Name Mode):")
                print(f"  {folder1_name}")
                print(f"  {folder2_name}")
                print(f"Location: {args.base_path}")
                print()
                
                simple_comparer = SimpleFileNameComparer(folder1_path, folder2_path)
                results = simple_comparer.compare_file_names()
                
                if results:
                    print(simple_comparer.generate_simple_report())
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    report_filename = f"ascf_simple_names_report_{timestamp}.txt"
                    simple_comparer.save_simple_report(report_filename)
                    print(f"Simple file name report saved to: {report_filename}")
                else:
                    print("Simple file name comparison failed. Check the folder paths and try again.")
            elif args.naming_convention:
                # Use naming convention comparison for ASCF patches
                folder1_name = "ASCF_HF_10240pb01hf04"
                folder2_name = "ASCF_HF_10240pb01hf05"
                
                folder1_path = os.path.join(args.base_path, folder1_name)
                folder2_path = os.path.join(args.base_path, folder2_name)
                
                print(f"Comparing ASCF patch folders (Naming Convention Mode):")
                print(f"  {folder1_name}")
                print(f"  {folder2_name}")
                print(f"Location: {args.base_path}")
                print(f"Similarity threshold: {args.similarity_threshold}")
                print()
                
                naming_comparer = NamingConventionComparer(folder1_path, folder2_path)
                naming_comparer.similarity_threshold = args.similarity_threshold
                results = naming_comparer.compare_naming_conventions()
                
                if results:
                    print(naming_comparer.generate_naming_convention_report())
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    report_filename = f"ascf_naming_convention_report_{timestamp}.txt"
                    naming_comparer.save_naming_convention_report(report_filename)
                    print(f"Naming convention report saved to: {report_filename}")
                else:
                    print("Naming convention comparison failed. Check the folder paths and try again.")
            else:
                compare_ascf_patches(args.base_path, args.max_retries, args.retry_delay, use_local_copy, not args.keep_temp_folders, args.ignore_timestamp_only, show_missing_only, args.exclude_missing_folder2, args.rename_folder2_only, args.rename_suffix)
        except Exception as e:
            print(f"Error: {e}")
            print()
            print("TROUBLESHOOTING NETWORK ACCESS ISSUES:")
            print("=" * 50)
            print("The most common issue is 'Access is denied' (Error 5)")
            print()
            print("IMMEDIATE SOLUTIONS:")
            print("1. Run the automated network setup helper:")
            print("   python setup_network_access.py")
            print()
            print("2. Map the network drive manually:")
            print(f"   net use Z: \"{args.base_path}\" /persistent:yes")
            print("   Then use: python main.py --base-path Z:")
            print()
            print("3. Connect with your domain credentials:")
            print(f"   net use \"{args.base_path}\" /user:DOMAIN\\username password")
            print("   (You'll be prompted for password)")
            print()
            print("4. Use Windows credential manager:")
            print("   Control Panel → Credential Manager → Add Windows credential")
            print(f"   Server: {args.base_path.split(chr(92))[2] if args.base_path.startswith(chr(92)+chr(92)) else 'server_name'}")
            print()
            print("OTHER TROUBLESHOOTING:")
            print("4. Check network path access and ensure the server is reachable")
            print("5. Verify folder names exist")
            print("6. Try --custom-folders for local folder comparison")
            print("7. Increase retry attempts with --max-retries 5")
            print("8. Increase retry delay with --retry-delay 2.0")
            print("9. Use --keep-temp-folders to preserve temporary folders for debugging")
            print()
            print("For testing local folders, use:")
            print("python main.py --custom-folders --folder1 C:\\path\\to\\folder1 --folder2 C:\\path\\to\\folder2")
            return 1
    
    return 0

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("Application interrupted.")
        sys.exit(0)
