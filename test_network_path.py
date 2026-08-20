#!/usr/bin/env python3
import os
import zipfile

src = r'\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\AmdocsCRM9.22.PB1.HFB11_release\release\SDK\Linux\SDK_CRM_9.22.PB1.HFB11_linux64_20251104_1431.jar'
print(f'Path: {src}')
print(f'Exists: {os.path.exists(src)}')
if os.path.exists(src):
    print(f'IsFile: {os.path.isfile(src)}')
    print(f'Size: {os.path.getsize(src) / (1024*1024):.1f} MB')
    
    # Test 1: Try opening directly with zipfile
    print('\nTest 1: Direct zipfile open...')
    try:
        with zipfile.ZipFile(src, 'r') as zf:
            print(f'  Success! {len(zf.namelist())} entries')
            # Print first 5 entries
            for i, name in enumerate(zf.namelist()[:5]):
                print(f'    [{i}] {name}')
    except Exception as e:
        print(f'  Failed: {type(e).__name__}: {e}')
        
        # Test 2: Try reading with binary and BytesIO
        print('\nTest 2: Reading binary and using BytesIO...')
        try:
            from io import BytesIO
            with open(src, 'rb') as f:
                data = f.read()
                print(f'  Read {len(data) / (1024*1024):.1f} MB')
            with zipfile.ZipFile(BytesIO(data), 'r') as zf:
                print(f'  Success! {len(zf.namelist())} entries')
        except Exception as e2:
            print(f'  Failed: {type(e2).__name__}: {e2}')




