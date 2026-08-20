#!/usr/bin/env python3
import subprocess
import os

archive_path = r'\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\AmdocsCRM9.22.PB1.HFB11_release\release\SDK\Linux\SDK_CRM_9.22.PB1.HFB11_linux64_20251104_1431.jar'

print(f'Testing PowerShell scan on: {archive_path}')
print(f'File exists: {os.path.exists(archive_path)}')

if os.path.exists(archive_path):
    # PowerShell command to list ZIP contents
    ps_cmd = f'''
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead("{archive_path.replace(chr(92), chr(92)+chr(92))}")
    $count = 0
    foreach ($entry in $zip.Entries) {{
        if (-not $entry.FullName.EndsWith("/")) {{
            Write-Output "$($entry.Length)|$($entry.Crc32)|$($entry.FullName)"
            $count++
            if ($count -ge 20) {{ break }}
        }}
    }}
    Write-Output "TOTAL_ENTRIES: $($zip.Entries.Count)"
    $zip.Dispose()
    '''
    print('\nRunning PowerShell command...')
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command', ps_cmd],
        capture_output=True, text=True, timeout=120
    )
    print(f'Return code: {result.returncode}')
    if result.stdout:
        print(f'\nOutput (first 20 entries):\n{result.stdout[:2000]}')
    if result.stderr:
        print(f'\nErrors:\n{result.stderr[:500]}')




