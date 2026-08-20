#!/usr/bin/env python3
import os
import zipfile
import shutil
import tempfile
import time

archive_path = r'\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\AmdocsCRM9.22.PB1.HFB11_release\release\SDK\Linux\SDK_CRM_9.22.PB1.HFB11_linux64_20251104_1431.jar'

print(f'Testing copy and scan on: {archive_path}')
print(f'File exists: {os.path.exists(archive_path)}')

if os.path.exists(archive_path):
    size_mb = os.path.getsize(archive_path) / (1024*1024)
    print(f'File size: {size_mb:.1f} MB')
    
    # Copy to temp
    print('\nCopying to temp...')
    start = time.time()
    with tempfile.NamedTemporaryFile(suffix='.jar', delete=False) as tmp:
        tmp_path = tmp.name
    
    try:
        shutil.copy2(archive_path, tmp_path)
        elapsed = time.time() - start
        print(f'Copy completed in {elapsed:.1f} seconds')
        
        # Try to scan the local copy
        print('\nScanning local copy with zipfile...')
        try:
            with zipfile.ZipFile(tmp_path, 'r') as zf:
                entries = zf.infolist()
                print(f'Successfully opened! Found {len(entries)} entries')
                
                # Show first 10 entries
                print('\nFirst 10 entries:')
                for i, info in enumerate(entries[:10]):
                    print(f'  [{i}] {info.filename} (size={info.file_size}, crc={info.CRC})')
                
                # Count nested archives
                nested = [e for e in entries if e.filename.lower().endswith(('.jar', '.war', '.ear', '.zip'))]
                print(f'\nFound {len(nested)} nested archives')
                if nested:
                    print('First 5 nested:')
                    for n in nested[:5]:
                        print(f'  {n.filename}')
        except Exception as e:
            print(f'Scan failed: {type(e).__name__}: {e}')
    finally:
        try:
            os.unlink(tmp_path)
            print('\nTemp file cleaned up')
        except:
            pass




