#!/usr/bin/env python3
"""
Network Access Setup Helper for ASCF Patch Comparison Tool

This script helps establish proper network credentials for accessing UNC paths.
"""

import subprocess
import sys
import getpass
from pathlib import Path
import argparse

# Default UNC path for ASCF patches (used by interactive and CLI modes)
DEFAULT_UNC = r"\\inpnqascflb02\uif_10241\uif"

def run_command(cmd, shell=True):
    """Run a system command and return result."""
    try:
        result = subprocess.run(cmd, shell=shell, capture_output=True, text=True)
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)

def check_current_connections():
    """Check current network drive mappings."""
    print("Checking current network connections...")
    success, stdout, stderr = run_command("net use")
    if success:
        if "The command completed successfully" in stdout and len(stdout.strip().split('\n')) <= 3:
            print("No active network connections found.")
            return []
        else:
            print("Current network connections:")
            print(stdout)
            return stdout.split('\n')
    else:
        print(f"Error checking connections: {stderr}")
        return []

def map_network_drive(unc_path, drive_letter="Z", username=None, password=None):
    """Map a network drive with credentials."""
    print(f"\nMapping {unc_path} to {drive_letter}:")
    
    # Build the command
    cmd = f'net use {drive_letter}: "{unc_path}"'
    
    if username:
        if not password:
            password = getpass.getpass(f"Enter password for {username}: ")
        cmd += f' /user:{username} "{password}"'
    
    cmd += " /persistent:yes"
    
    print(f"Executing: net use {drive_letter}: \"{unc_path}\" /user:{username or '[current user]'} [password] /persistent:yes")
    
    success, stdout, stderr = run_command(cmd)
    
    if success:
        print("✓ Network drive mapped successfully!")
        return True
    else:
        print(f"✗ Failed to map network drive: {stderr}")
        return False

def unmap_network_drive(drive_letter="Z"):
    """Remove a mapped network drive."""
    print(f"\nRemoving mapping for {drive_letter}:")
    cmd = f"net use {drive_letter}: /delete /y"
    success, stdout, stderr = run_command(cmd)
    if success:
        print("✓ Network drive unmapped successfully!")
        return True
    else:
        print(f"✗ Failed to unmap network drive: {stderr}")
        return False

def test_access(path):
    """Test access to a path."""
    test_path = Path(path)
    try:
        if test_path.exists():
            contents = list(test_path.iterdir())
            print(f"✓ Successfully accessed {path} ({len(contents)} items found)")
            return True
        else:
            print(f"✗ Path does not exist: {path}")
            return False
    except Exception as e:
        print(f"✗ Cannot access {path}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Network Access Setup Helper")
    parser.add_argument("--map", action="store_true", help="Map the UNC path to a drive letter")
    parser.add_argument("--unmap", action="store_true", help="Unmap the drive letter")
    parser.add_argument("--unc", default=DEFAULT_UNC, help="UNC path to map (default: configured UNC)")
    parser.add_argument("--drive", default="Z", help="Drive letter to use (default: Z)")
    parser.add_argument("--username", help="Username for mapping (DOMAIN\\user or user@domain). Omit to use current user")
    parser.add_argument("--password", help="Password for mapping (use with --username). If omitted, will prompt interactively")
    parser.add_argument("--test", action="store_true", help="Test access to the mapped drive or UNC")
    parser.add_argument("--list", action="store_true", help="List current network connections")

    args, unknown = parser.parse_known_args()

    # CLI mode if any of the control flags are used
    if args.map or args.unmap or args.test or args.list:
        print("=" * 60)
        print("NETWORK ACCESS SETUP - CLI MODE")
        print("=" * 60)

        rc = 0

        if args.list:
            check_current_connections()

        if args.unmap:
            if not unmap_network_drive(args.drive):
                rc = 1

        if args.map:
            # If username provided but no password, prompt securely
            password = args.password
            if args.username and password is None:
                password = getpass.getpass(f"Enter password for {args.username}: ")
            if not map_network_drive(args.unc, args.drive, args.username, password):
                rc = 1

        if args.test:
            target = f"{args.drive}:" if args.map or args.unmap else (f"{args.drive}:" if Path(f"{args.drive}:").exists() else args.unc)
            if not test_access(target):
                rc = 1

        return rc

    # Interactive mode (legacy)
    print("=" * 60)
    print("NETWORK ACCESS SETUP HELPER")
    print("=" * 60)
    
    print(f"This tool will help you establish access to: {DEFAULT_UNC}")
    print()
    
    # Check current connections
    check_current_connections()
    print()
    
    # Test current access
    print("Testing current access...")
    if test_access(DEFAULT_UNC):
        print("✓ You already have access! You can run the main tool now.")
        return 0
    
    print("\nAccess failed. Let's set up network credentials.")
    print()
    
    # Options for the user
    print("Choose an option:")
    print("1. Map network drive with current Windows credentials")
    print("2. Map network drive with specific domain credentials")
    print("3. Just display the manual commands to run")
    print("4. Exit")
    
    choice = input("\nEnter choice (1-4): ").strip()
    
    if choice == "1":
        print("\nTrying with current Windows credentials...")
        if map_network_drive(DEFAULT_UNC):
            print("\nTesting access with mapped drive...")
            if test_access("Z:"):
                print("\n✓ Success! You can now use: python main.py --base-path Z:")
            else:
                print("\n✗ Mapping succeeded but still can't access files.")
                print("You may need to use option 2 with domain credentials.")
    
    elif choice == "2":
        print("\nEnter domain credentials:")
        print("Format examples:")
        print("  DOMAIN\\username")
        print("  username@domain.com")
        print("  username (if same domain)")
        
        username = input("\nUsername: ").strip()
        if username:
            if map_network_drive(DEFAULT_UNC, username=username):
                print("\nTesting access with mapped drive...")
                if test_access("Z:"):
                    print("\n✓ Success! You can now use: python main.py --base-path Z:")
                else:
                    print("\n✗ Mapping succeeded but still can't access files.")
                    print("Contact IT support for help with permissions.")
    
    elif choice == "3":
        print("\nMANUAL COMMANDS TO RUN:")
        print("=" * 40)
        print("1. Open Command Prompt as Administrator")
        print("2. Run one of these commands:")
        print()
        print("   Option A - Use current credentials:")
        print(f'   net use Z: "{DEFAULT_UNC}" /persistent:yes')
        print()
        print("   Option B - Use domain credentials:")
        print(f'   net use Z: "{DEFAULT_UNC}" /user:DOMAIN\\username /persistent:yes')
        print("   (You'll be prompted for password)")
        print()
        print("3. Then run the main tool:")
        print("   python main.py --base-path Z:")
        print()
        print("4. To remove the mapping later:")
        print("   net use Z: /delete")
    
    elif choice == "4":
        print("Exiting...")
        return 0
    
    else:
        print("Invalid choice. Exiting...")
        return 1
    
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
        sys.exit(0)

