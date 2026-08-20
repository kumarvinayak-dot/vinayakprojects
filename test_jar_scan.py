#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
from crm_compare import CRMFolderComparer
import time

old_jar = r'\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\AmdocsCRM9.22.PB1.HFB11_release\release\SDK\Linux\SDK_CRM_9.22.PB1.HFB11_linux64_20251104_1431.jar'
new_jar = r'\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\AmdocsCRM9.22.PB1.HFB12_release\release\SDK\Linux\SDK_CRM_9.22.PB1.HFB12_linux64_20251210_1232.jar'

print('Creating comparer...')
comparer = CRMFolderComparer(old_jar, new_jar)

print('Scanning old JAR...')
files1 = comparer._get_all_files(comparer.folder1_path)
print(f'Found {len(files1)} files in old JAR')
if files1:
    sample = list(files1.items())[:5]
    print('Sample files:')
    for k, v in sample:
        print(f'  {k[:80]}... -> size={v.get("size")}, crc={v.get("crc")}')

print('\nScanning new JAR...')
files2 = comparer._get_all_files(comparer.folder2_path)
print(f'Found {len(files2)} files in new JAR')
if files2:
    sample = list(files2.items())[:5]
    print('Sample files:')
    for k, v in sample:
        print(f'  {k[:80]}... -> size={v.get("size")}, crc={v.get("crc")}')

# Check for differences
all_keys = set(files1.keys()) | set(files2.keys())
only_in_1 = set(files1.keys()) - set(files2.keys())
only_in_2 = set(files2.keys()) - set(files1.keys())
common = set(files1.keys()) & set(files2.keys())

print(f'\n=== COMPARISON SUMMARY ===')
print(f'Files only in old JAR: {len(only_in_1)}')
print(f'Files only in new JAR: {len(only_in_2)}')
print(f'Files in both: {len(common)}')

# Check CRC differences for common files
crc_different = 0
for k in common:
    crc1 = files1[k].get('crc', 0)
    crc2 = files2[k].get('crc', 0)
    if crc1 != crc2:
        crc_different += 1
        if crc_different <= 5:
            print(f'  CRC differs: {k[:70]}... ({crc1} vs {crc2})')

print(f'Files with different CRC: {crc_different}')

# Show some files only in old
if only_in_1:
    print('\nSample files only in OLD:')
    for k in list(only_in_1)[:5]:
        print(f'  [-] {k[:80]}...')

# Show some files only in new
if only_in_2:
    print('\nSample files only in NEW:')
    for k in list(only_in_2)[:5]:
        print(f'  [+] {k[:80]}...')




