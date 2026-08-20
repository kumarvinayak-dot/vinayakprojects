# Size Mismatch Detection Feature

## Summary
Added a new **Size Mismatches** section to archive comparison reports that shows files with different sizes between two archives.

## What It Does

When comparing two JAR/WAR/EAR/ZIP files, the system now:
1. **Detects size differences** - Identifies files that exist in both archives with the same name but different sizes
2. **Shows detailed comparison** - Displays sizes from both archives side-by-side
3. **Calculates differences** - Shows absolute size difference and percentage change
4. **Provides navigation** - Adds a hyperlink in the navigation menu to jump to size mismatches

## Features

### 1. **Navigation Link**
- Added to the Quick Navigation menu
- Shows count of files with size mismatches
- Example: `📏 Size Mismatches (45)`
- Color-coded with orange border

### 2. **Dedicated Section**
A new section in the HTML report with:
- **Table format** showing:
  - File name
  - Size in Archive 1
  - Size in Archive 2
- **Formatted sizes** using appropriate units (B, KB, MB, GB)
- **Difference calculation** showing:
  - Absolute difference
  - Percentage change
  - Direction indicator (+/-)

### 3. **Visual Design**
- Professional table layout
- Monospace font for size values (easy comparison)
- Warning color theme (orange)
- Alternating rows for difference details
- Highlighted difference row with percentage

## Example Output

### Navigation Menu
```
📑 Quick Navigation
├─ 🆕 Additional/New Files (5)
├─ ⚠️ Content Differs (120)
├─ 📏 Size Mismatches (45)  ← NEW!
├─ 📊 Detailed Comparison Table
├─ ❌ Missing from Archive 2 (10)
├─ ✅ Missing from Archive 1 (8)
└─ ✓ Exact Matches (3532)
```

### Size Mismatch Section
```
📏 Size Mismatches (45 files)
Files that exist in both archives with the same name but different sizes.

┌─────────────────────────────────┬───────────────┬───────────────┐
│ File Name                       │ Archive 1 Size│ Archive 2 Size│
├─────────────────────────────────┼───────────────┼───────────────┤
│ com/example/Service.class       │    15.42 KB   │    16.18 KB   │
│         Difference: 775 B (+4.9%)                               │
├─────────────────────────────────┼───────────────┼───────────────┤
│ lib/dependency.jar              │    2.35 MB    │    2.41 MB    │
│         Difference: 61.44 KB (+2.6%)                            │
└─────────────────────────────────┴───────────────┴───────────────┘
```

## Technical Implementation

### Data Collection
In `builtin_server.py`, during archive comparison processing:

```python
# Track size mismatches
size_mismatches = {}  # full_name -> (size1, size2)

# For each member in both archives
for member in members1 & members2:
    m1 = meta1.get(member, (0, 0))
    m2 = meta2.get(member, (0, 0))
    size1, crc1 = m1
    size2, crc2 = m2
    
    if size1 != size2:
        size_mismatches[full_name] = (size1, size2)
```

### Size Formatting Function
```python
def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
```

### HTML Table Generation
Creates a professional table with:
- File name column (50% width)
- Archive 1 size column (25% width, right-aligned)
- Archive 2 size column (25% width, right-aligned)
- Difference row spanning all columns

## Use Cases

### 1. **Version Comparison**
Compare `SDK_CRM_10.0.1.15.jar` vs `SDK_CRM_10.0.1.14.jar`
- Quickly identify which classes/resources changed size
- Detect potential bloat or optimization opportunities

### 2. **Build Verification**
Ensure critical files haven't unexpectedly changed size
- Configuration files should remain stable
- Core libraries with size changes need investigation

### 3. **Regression Detection**
Monitor for unexpected size increases
- Large size increases may indicate issues
- Size decreases might indicate missing functionality

### 4. **Dependency Analysis**
Track third-party library size changes
- Understand impact of dependency updates
- Identify opportunities for slimming down

## Files Modified

**`builtin_server.py`:**
1. Added `size_mismatches = {}` dictionary in archive processing
2. Enhanced member comparison to track size differences
3. Updated `_generate_archive_comparison_html_table()` signature to accept `size_mismatches`
4. Added navigation link for size mismatches section
5. Added size mismatches HTML section with table

## Testing

### To Test:
1. **Restart server:**
   ```bash
   py builtin_server.py --host 10.19.164.180 --port 8080 --skip-demos
   ```

2. **Run archive comparison:**
   - Mode: Archive contents
   - Compare two JAR files with known size differences
   - Example: HFB15 vs HFB14

3. **Check HTML report:**
   - ✅ Navigation menu shows "📏 Size Mismatches (X)"
   - ✅ Click the link to jump to the section
   - ✅ Table displays file names and sizes correctly
   - ✅ Sizes are formatted with appropriate units
   - ✅ Differences are calculated accurately
   - ✅ Percentage changes are shown

### Expected Debug Output:
```
[DEBUG] Archive comparison results: 1 entries
[DEBUG] Processing 1 archive entries for HTML table generation
[DEBUG] Entry: ... exists_in_1=True, exists_in_2=True
[DEBUG] Archive 1 has 3500 members, Archive 2 has 3495 members
[DEBUG] Final stats: ... size_mismatches=45
[DEBUG] Writing archive HTML report to: ...
```

## Benefits

✅ **Better visibility** - Size differences are now prominent and easy to find  
✅ **Detailed information** - Shows exact sizes and differences  
✅ **Easy navigation** - Hyperlink in navigation menu  
✅ **Professional formatting** - Human-readable size units  
✅ **Actionable insights** - Percentage changes help prioritize investigation  
✅ **Comprehensive comparison** - Works with all archive formats (JAR, WAR, EAR, ZIP)

## Comparison with "Content Differs"

**Content Differs:** Files with ANY metadata difference (size OR CRC)  
**Size Mismatches:** Specifically files with SIZE differences (more focused)

Both sections are useful:
- **Content Differs** = broad view of all differences
- **Size Mismatches** = detailed size-specific analysis

Files with size differences appear in both sections, but Size Mismatches provides the actual size values for analysis.

