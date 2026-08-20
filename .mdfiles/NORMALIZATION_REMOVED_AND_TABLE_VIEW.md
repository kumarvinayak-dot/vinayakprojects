# Normalization Removed from Archive Comparison + HTML Table View

## Summary
Removed normalization logic from archive comparison and implemented HTML table view for archive member comparison.

## Changes Made

### 1. Removed Normalization from `src/folder_compare.py`

#### Direct Archive-to-Archive Comparison (Lines 2459-2478)
**Before:**
- Used `_normalize_member_set()` to normalize file names
- Compared normalized versions (treating files with different version numbers as identical)
- Used `_normalize_inner_archive_marker()` to find original names

**After:**
- Direct comparison of actual member names (no normalization)
- Files with different version numbers are now correctly identified as different
- Metadata comparison uses actual member names

#### Regular Archive Comparison in Folders (Lines 2546-2564)
**Before:**
- Used `_normalize_member_set()` for both archives
- Compared normalized member sets
- Used `_normalize_inner_archive_marker()` to match members

**After:**
- Direct comparison without normalization
- Uses actual member names for comparison
- Detects all naming convention differences

### 2. Removed Quick Actions from GUI (`builtin_server.py`)
- Removed the Quick Actions section containing pre-configured comparison buttons
- Cleaner UI with just the comparison form and reports link

### 3. Added HTML Table View for Archive Comparison (`builtin_server.py`)

#### New Method: `_generate_archive_comparison_html_table()`
Generates a comprehensive HTML report for archive member comparison with:

**Features:**
- **Header Section**: Shows both archive paths
- **Navigation Menu**: Quick links to different sections
- **Summary Cards**: Total members, exact matches, differences
- **Additional/New Files Section**: Files unique to each archive
- **Content Differs Section**: Files with same name but different content/metadata
- **Detailed Comparison Table**: Side-by-side view of all members
- **Missing Files Sections**: Separate lists for files missing from each archive
- **Exact Matches Section**: Expandable list of identical files
- **Color Coding**:
  - 🔵 Blue = Identical files
  - 🔴 Red = Missing in right archive
  - 🟢 Green = Missing in left archive
  - 🟡 Yellow = Additional/New files
  - ⚠️ Orange = Content differs

#### Integration in Archive Comparison Mode
When mode == "archives":
1. Runs the archive comparison
2. Extracts member lists from comparison results
3. Categorizes members into:
   - Only in archive 1
   - Only in archive 2
   - In both archives (identical)
   - In both but content differs
4. Identifies truly new files (base name doesn't exist in other archive)
5. Generates HTML table view report
6. Saves both text and HTML reports

## Impact

### For Users
✅ **Archive comparisons now show actual differences** instead of hiding them through normalization
✅ **JAR file version differences are detected** (e.g., SDK_CRM_10.0.1.22 vs SDK_CRM_10.0.1.21)
✅ **Beautiful HTML table view** similar to Beyond Compare for archive contents
✅ **Easy navigation** with quick links to different sections
✅ **Expandable sections** for large lists of matching files

### Example Use Case
When comparing:
- `SDK_CRM_10.0.1.15_linux64_20250119_1651.jar`
- `SDK_CRM_10.0.1.14_linux64_20241007_1111.jar`

**Before:** "No differences found" (due to normalization)
**After:** Shows all actual differences including:
- Files with different version numbers in names
- Files only in one JAR
- Files in both but with different content

## Testing

Compile check: ✅ Passed
- `src/folder_compare.py` compiles successfully
- `builtin_server.py` compiles successfully

## Usage

Start the server:
```bash
py builtin_server.py --host 10.19.164.180 --port 8080 --skip-demos
```

Then compare archives:
1. Select "Archive contents" comparison mode
2. Enter paths to two JAR/WAR/EAR/ZIP files
3. Click "Run Comparison"
4. View both text report and HTML table view

## Files Modified
1. `src/folder_compare.py` - Removed normalization from `compare_archives()`
2. `builtin_server.py` - Added HTML table view generation for archives

