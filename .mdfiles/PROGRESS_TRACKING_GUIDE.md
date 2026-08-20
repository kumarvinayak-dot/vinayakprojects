# Progress Tracking and Timeout Handling Guide

## Overview

The comparison tool now includes comprehensive progress tracking with a real-time progress bar in the web UI, time estimates, and automatic timeout handling that generates partial reports when comparisons fail or get stuck.

## Key Features

### 1. Real-Time Progress Bar

When you run a comparison through the web UI, you'll see:
- **Progress percentage**: Visual bar showing % completion
- **Files processed**: Count of files processed vs. total files
- **Current file**: The file currently being compared
- **Estimated time remaining**: Dynamic ETA that updates as comparison progresses

### 2. Automatic Timeout Handling

The comparison tool includes two timeout mechanisms:

#### Overall Comparison Timeout
- **Default**: 2 hours (7200 seconds)
- **Behavior**: If the entire comparison takes longer than this, it will stop and generate a partial report

#### Per-File Timeout
- **Default**: 10 minutes (600 seconds) per file
- **Behavior**: If a single file takes longer than this to process, the tool will log an error and move to the next file

### 3. Error Reporting

When a comparison fails or times out:
- **Partial reports are generated** with all results up to the point of failure
- **Error messages are displayed** in the report showing:
  - The specific error that occurred
  - Number of files processed before the error
  - A note that this is a partial report

### 4. Network Error Handling

The tool includes robust network error handling:
- **Automatic retries** for temporary network issues
- **Network connectivity checks** before file operations
- **Clear error messages** when network paths are unavailable
- **Graceful exit** with report generation before terminating

## Using the Progress Bar

### In the Web UI

1. Navigate to `http://localhost:5000` (or your configured host:port)
2. Enter your folder paths and select comparison mode
3. Click "Run comparison"
4. The progress bar will automatically appear below the form
5. Watch real-time progress with:
   - Percentage complete
   - Files processed count
   - Current file being compared
   - Estimated time remaining

### Progress Updates

The progress bar updates every second, showing:
```
Comparison in Progress...
[==================>     ] 75.3%

Status: Running...
Files processed: 1508 / 2000
Current file: path/to/current/file.jar
Estimated time remaining: 2m 15s
```

### If an Error Occurs

If the comparison encounters an error:
```
Error: Comparison timeout after 7200 seconds
Files processed before error: 1508/2000
Note: This is a PARTIAL report with results up to the point of error
```

The tool will:
1. Display the error in the progress bar
2. Generate a partial report with all results collected so far
3. Mark the report as "INCOMPLETE/ERROR" at the top
4. Save both text and HTML reports with error details

## Timeout Configuration

You can customize timeout values in the code:

```python
from folder_compare import FolderComparer

comparer = FolderComparer(folder1, folder2)

# Customize timeouts
comparer.comparison_timeout = 3600  # 1 hour overall timeout
comparer.file_timeout = 300  # 5 minutes per file

results = comparer.compare_folders()
```

## Common Scenarios

### Scenario 1: Network Path is Slow

If you're comparing network paths that are very slow:

**Symptoms:**
- Progress bar shows very slow advancement
- Each file takes a long time

**Solutions:**
1. Increase timeouts:
   ```python
   comparer.comparison_timeout = 14400  # 4 hours
   comparer.file_timeout = 1800  # 30 minutes per file
   ```
2. Use local copies if possible
3. Enable the network copy feature in the web UI

### Scenario 2: Comparison Gets Stuck

If comparison appears stuck on a specific file:

**Symptoms:**
- Progress bar shows same file for >10 minutes
- No advancement in percentage

**Automatic Handling:**
- After 10 minutes (default), the tool will:
  1. Log a timeout error for that file
  2. Continue to the next file
  3. Include timeout info in the report

### Scenario 3: Network Connection Lost

If network connection is lost during comparison:

**Symptoms:**
- Progress stops advancing
- Errors appear in progress bar

**Automatic Handling:**
- The tool will attempt retries (default: 3 attempts)
- If all retries fail, it will:
  1. Generate a partial report
  2. Show network error details
  3. Exit gracefully

### Scenario 4: Very Large Directories

For very large directories with thousands of files:

**Best Practices:**
1. **Use the web UI** for real-time progress visibility
2. **Monitor the ETA** to plan your time
3. **Enable "Suppress warnings"** to reduce log noise
4. **Consider splitting** the comparison into smaller chunks

**Example Report Header:**
```
FOLDER COMPARISON REPORT
================================================================================
Folder 1: \\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06
Folder 2: \\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07
Comparison Date: 2025-11-24 15:30:45

*** COMPARISON STATUS: COMPLETE ***
```

Or if there was an error:
```
*** COMPARISON STATUS: INCOMPLETE/ERROR ***
Error: Comparison timeout after 7200 seconds
Files processed before error: 1508/2000
Note: This is a PARTIAL report with results up to the point of error
```

## API Reference

### FolderComparer Progress Properties

```python
comparer = FolderComparer(folder1, folder2)

# Progress tracking properties (read-only, updated during comparison)
comparer.total_files          # Total number of files to compare
comparer.processed_files      # Number of files processed so far
comparer.current_file         # Current file being compared
comparer.progress_percentage  # Percentage complete (0-100)
comparer.estimated_time_remaining  # Seconds remaining (estimated)
comparer.comparison_error     # Error message if comparison failed

# Timeout configuration (set before calling compare_folders)
comparer.comparison_timeout = 7200  # Overall timeout in seconds
comparer.file_timeout = 600         # Per-file timeout in seconds
```

### Progress Endpoint

The web UI polls `/progress` endpoint every second:

```json
{
  "running": true,
  "progress": 1508,
  "total": 2000,
  "percentage": 75.4,
  "current_file": "path/to/file.jar",
  "eta_seconds": 135,
  "eta_formatted": "2m 15s",
  "error": null
}
```

## Tips for Best Performance

1. **Use network copy feature** for network paths to copy to local temp folder first
2. **Enable "Suppress warnings"** to reduce logging overhead
3. **Close other applications** that might be accessing the same network paths
4. **Run during off-peak hours** for network comparisons
5. **Monitor the progress bar** to identify slow files/patterns

## Troubleshooting

### Progress Bar Not Appearing

**Check:**
- JavaScript is enabled in your browser
- You're using a modern browser (Chrome, Firefox, Edge)
- Console for any JavaScript errors (F12 → Console)

### ETA Shows "Calculating..." for a Long Time

**Reason:** ETA calculation requires at least 1 file to be processed to estimate

**Solution:** Wait for the first file to complete, then ETA will appear

### Progress Stops at 99%

**Reason:** Final file might be very large or complex archive

**Solution:** Wait for file timeout (10 min default) or check logs for specific file issues

## Command-Line Progress

When running from command line, progress is logged to console:

```bash
python main.py compare "\\path\to\folder1" "\\path\to\folder2" -m full
```

**Output:**
```
Comparing folders: \\path\to\folder1 vs \\path\to\folder2
Found 1500 files in folder 1, 1450 files in folder 2
Total files to compare: 2000
Progress: 100/2000 files processed (5.0%) - ETA: 450s
Progress: 200/2000 files processed (10.0%) - ETA: 405s
...
```

## Summary

The new progress tracking system provides:
- ✅ Real-time visual feedback
- ✅ Accurate time estimates
- ✅ Automatic timeout handling
- ✅ Partial report generation on errors
- ✅ Network error resilience
- ✅ Graceful error messages

This ensures you always get useful results, even when comparisons encounter issues or take a very long time.

