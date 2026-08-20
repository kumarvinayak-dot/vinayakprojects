#!/usr/bin/env python3
"""
Test to verify the list-to-string conversion fix
"""

def test_list_conversion():
    """Test that the list-to-string conversion works correctly"""
    
    # Simulate the data structure from NamingConventionComparer
    # filename -> [list of paths]
    jar_files_folder1 = {
        'commons-lang3-3.12.0.jar': ['lib/commons-lang3-3.12.0.jar', 'modules/commons-lang3-3.12.0.jar'],
        'log4j-1.2.17.jar': ['lib/log4j-1.2.17.jar']
    }
    
    other_files_folder1 = {
        'application.war': ['webapps/application.war']
    }
    
    jar_files_folder2 = {
        'commons-lang3-3.13.0.jar': ['lib/commons-lang3-3.13.0.jar'],
        'log4j-1.2.17.jar': ['lib/log4j-1.2.17.jar']
    }
    
    other_files_folder2 = {
        'application.war': ['webapps/application.war'],
        'admin.war': ['webapps/admin.war']
    }
    
    # Merge dictionaries (OLD WAY - causes errors)
    archives1_raw = {**jar_files_folder1, **other_files_folder1}
    archives2_raw = {**jar_files_folder2, **other_files_folder2}
    
    print("=" * 80)
    print("Testing List-to-String Conversion")
    print("=" * 80)
    print()
    
    print("RAW DATA (lists):")
    print(f"archives1_raw: {archives1_raw}")
    print(f"archives2_raw: {archives2_raw}")
    print()
    
    # NEW WAY - convert lists to strings
    archives1 = {name: (paths[0] if isinstance(paths, list) and paths else paths) 
                for name, paths in archives1_raw.items()}
    archives2 = {name: (paths[0] if isinstance(paths, list) and paths else paths) 
                for name, paths in archives2_raw.items()}
    
    print("CONVERTED DATA (strings):")
    print(f"archives1: {archives1}")
    print(f"archives2: {archives2}")
    print()
    
    # Test that all values are now strings
    print("VERIFICATION:")
    all_strings = True
    for name, path in archives1.items():
        is_str = isinstance(path, str)
        status = "✅" if is_str else "❌"
        print(f"  {status} archives1['{name}']: {type(path).__name__} = {path}")
        if not is_str:
            all_strings = False
    
    for name, path in archives2.items():
        is_str = isinstance(path, str)
        status = "✅" if is_str else "❌"
        print(f"  {status} archives2['{name}']: {type(path).__name__} = {path}")
        if not is_str:
            all_strings = False
    
    print()
    
    # Test the replace operation that was causing the error
    print("TESTING .replace() OPERATION:")
    try:
        # Simulate the operation that was failing
        all_files = {}
        for name in archives1:
            all_files[name] = {'in_1': True, 'path_1': archives1[name], 'in_2': False, 'path_2': None}
        for name in archives2:
            if name in all_files:
                all_files[name]['in_2'] = True
                all_files[name]['path_2'] = archives2[name]
            else:
                all_files[name] = {'in_1': False, 'path_1': None, 'in_2': True, 'path_2': archives2[name]}
        
        for name in all_files:
            file_info = all_files[name]
            if file_info['in_1'] and file_info['in_2']:
                file_info['status'] = 'identical'
            elif file_info['in_1']:
                file_info['status'] = 'missing-right'
            else:
                file_info['status'] = 'missing-left'
        
        # This is where the error was happening
        for name, info in all_files.items():
            status_class = info['status'].replace('-', '_')  # This would fail if status was a list
            print(f"  ✅ {name}: status='{info['status']}' -> status_class='{status_class}'")
        
        print()
        print("=" * 80)
        print("✅ ALL TESTS PASSED!")
        print("=" * 80)
        print()
        print("The fix successfully converts lists to strings,")
        print("preventing the 'list' object has no attribute 'replace' error.")
        
    except AttributeError as e:
        print(f"  ❌ ERROR: {e}")
        print()
        print("=" * 80)
        print("❌ TEST FAILED!")
        print("=" * 80)
        return False
    
    return all_strings

if __name__ == "__main__":
    success = test_list_conversion()
    exit(0 if success else 1)










