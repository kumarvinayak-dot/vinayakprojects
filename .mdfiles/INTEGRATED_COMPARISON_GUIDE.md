# Integrated Comparison Guide

## Overview

All comparison logics have now been integrated so you can get comprehensive results in a single shot!

## What's Been Integrated

### ✅ Full Content Comparison (FolderComparer)
Now includes:
1. **File-level comparison** (size, timestamps, hashes)
2. **Deep archive contents** comparison with normalization
3. **Version pattern normalization** (__V10_0_1_22, __V10_0_1_21 → normalized)
4. **Pack200 normalization** (.jar.pack.gz files normalized)
5. **Unsigned JAR normalization** (_unsigned suffix handled)
6. **JAR manifest comparison** (Implementation-Version)
7. **Nested archive support** (recurses into JARs within JARs)

### ✅ Archive Contents Comparison (ArchiveContentsComparer)
Specialized for:
1. **Direct JAR-to-JAR** comparison
2. **Deep nested inspection** (EAR → WAR → JAR)
3. **Member-level comparison** with normalization
4. **Java jar command fallback** for corrupted ZIP extra fields

---

## Usage Examples

### Scenario 1: Compare Two Directories

Use `FolderComparer` for full recursive directory comparison with all features:

```python
from folder_compare import FolderComparer

comparer = FolderComparer("path/to/folder1", "path/to/folder2")
comparer.compare_archive_contents = True  # Enable archive normalization
comparer.show_missing_only = False  # Show all differences
results = comparer.compare_folders()

# Generate report
comparer.save_report("comparison_report.txt")
html_content = comparer.generate_html_report()
with open("comparison_report.html", "w") as f:
    f.write(html_content)
```

**Features automatically included:**
- ✅ All files compared
- ✅ Archives (JAR/WAR/EAR/ZIP/RPM) deeply compared
- ✅ Version normalization applied to archive members
- ✅ .pack.gz files normalized
- ✅ Single comprehensive report

---

### Scenario 2: Compare Two JAR Files

Use `ArchiveContentsComparer` for direct JAR-to-JAR comparison:

```python
from folder_compare import ArchiveContentsComparer

comparer = ArchiveContentsComparer(
    "SDK_CRM_10.0.1.22_linux64_20251120_2043.jar",
    "SDK_CRM_10.0.1.21_linux64_20250908_1434.jar"
)
comparer.deep_nested_archives = True  # Recurse into nested JARs

results = comparer.compare_archives()

# Generate reports
comparer.save_archive_report("jar_comparison.txt")
html_content = comparer.generate_archive_html_report()
with open("jar_comparison.html", "w") as f:
    f.write(html_content)
```

**Features automatically included:**
- ✅ Member list comparison with normalization
- ✅ Content (size/CRC) comparison
- ✅ Nested JAR recursion
- ✅ .pack.gz handling
- ✅ Java jar command fallback for corrupt files

---

### Scenario 3: Command-Line Full Comparison

```bash
# Compare directories with all features
py main.py --custom-folders \
  --folder1 "path/to/release1" \
  --folder2 "path/to/release2" \
  --show-all-differences

# Compare JAR files
py compare_crm_jars.py
```

---

### Scenario 4: Web Interface

```bash
# Start server
.\.venv\Scripts\python.exe builtin_server.py --host 0.0.0.0 --port 5000
```

Then in browser:
1. **Full Comparison Mode**: Enter folder paths, select "Full content comparison"
2. **Archive Mode**: Select "Archives (.ear/.war/.zip/.rpm/.jar) contents"
3. **Quick Actions**: Click "Compare CRM SDK JARs" button

---

## Normalization Patterns Handled

### 1. CRM Version Patterns
```
winpack__V10_0_1_22.jar  → winpack.jar
winpack__V10_0_1_21.jar  → winpack.jar
✅ Treated as IDENTICAL
```

### 2. Embedded Versions
```
SDK_CRM_10.0.1.22_linux64_20251120_2043.jar → SDK_CRM_linux64.jar
SDK_CRM_10.0.1.21_linux64_20250908_1434.jar → SDK_CRM_linux64.jar
✅ Treated as IDENTICAL
```

### 3. Pack200 Compressed Files
```
file__V10_0_1_22.jar.pack.gz → file.jar.pack.gz
file__V10_0_1_21.jar.pack.gz → file.jar.pack.gz
✅ Treated as IDENTICAL
```

### 4. Unsigned JAR Suffix
```
module_unsigned.jar → module.jar
module.jar          → module.jar
✅ Treated as IDENTICAL
```

### 5. Timestamps
```
build_20251120_2043.jar → build.jar
build_20250908_1434.jar → build.jar
✅ Treated as IDENTICAL
```

---

## Report Features

### Text Reports Include:
- ✅ Summary with normalization status
- ✅ Missing files list
- ✅ Different files with details
- ✅ Archive contents differences (normalized)
- ✅ JAR manifest versions
- ✅ Member-level details

### HTML Reports Include:
- ✅ Interactive navigation
- ✅ Expandable sections
- ✅ Color-coded differences
- ✅ Normalization notes
- ✅ Download links

---

## Configuration Options

### FolderComparer Options

```python
comparer = FolderComparer(folder1, folder2)

# Archive comparison
comparer.compare_archive_contents = True  # Enable deep archive comparison

# Display options
comparer.show_missing_only = False  # Show all diffs, not just missing
comparer.suppress_warnings = False  # Show all warnings

# Network options
comparer.use_local_copy = False  # Work directly with network paths
comparer.max_retries = 3  # Retry count for network errors

# Filtering options
comparer.ignore_timestamp_only_differences = False  # Count timestamp diffs
comparer.exclude_missing_from_folder2 = False  # Exclude missing in folder2
```

### ArchiveContentsComparer Options

```python
comparer = ArchiveContentsComparer(jar1, jar2)

# Recursion
comparer.deep_nested_archives = True  # Recurse into nested archives

# Logging
comparer.suppress_warnings = False  # Show warnings
```

---

## Performance Considerations

### For Large Directories:
- Use `show_missing_only = True` to reduce report size
- Consider `use_local_copy = True` for network paths
- Archives are compared efficiently (member lists, not full extraction)

### For Large JARs:
- Normalization adds ~1 minute per 3000 members
- Java jar command fallback adds ~2 seconds per JAR
- Deep nesting (EAR→WAR→JAR) is supported but slower

---

## Troubleshooting

### "Corrupt extra field" Errors
✅ **Fixed!** Now falls back to Java `jar` command automatically
- Requires Java JDK to be installed
- No manual intervention needed

### "No differences found" But Files Look Different
✅ **Check normalization** - versions like V10_0_1_22 vs V10_0_1_21 are intentionally treated as identical
- Review the normalized member lists in the report
- Check if differences are only in version numbers

### Slow Comparison
- Disable `compare_archive_contents` if you only need file-level comparison
- Use `show_missing_only` to speed up reporting
- Consider comparing on local copies instead of network paths

---

## Example Workflows

### Release Comparison Workflow
```bash
# 1. Run full comparison
py main.py --custom-folders \
  --folder1 "\\server\share\HFB22_release" \
  --folder2 "\\server\share\HFB21_release" \
  --show-all-differences

# 2. Review generated report
# - Look for "Archive normalization: ENABLED" in summary
# - Check "ARCHIVE FILES WITH DIFFERENCES" section
# - Version differences will show as normalized

# 3. Focus on real differences
# - Files with "Members with different content (size/CRC)"
# - Files only in one release (after normalization)
```

### JAR Comparison Workflow
```bash
# 1. Compare specific JARs
py compare_crm_jars.py

# 2. Review results
# - "No differences found" = functionally identical (after normalization)
# - "Archives with member list differences: 0" = all members match
# - "Archives with file content differences: X" = real content changes

# 3. Deep dive if needed
# - Check HTML report for detailed member lists
# - Look for content CRC differences
```

---

## Summary

**All comparison features are now integrated!**

- ✅ **Single command** gets you everything
- ✅ **Normalization** automatically applied to archives
- ✅ **Java jar fallback** handles corrupt files
- ✅ **.pack.gz support** for compressed JARs
- ✅ **Comprehensive reports** with all details
- ✅ **Web interface** with quick actions

**Choose your tool:**
- **FolderComparer**: For directory comparisons
- **ArchiveContentsComparer**: For direct JAR-to-JAR comparison
- **main.py**: Command-line interface
- **builtin_server.py**: Web-based interface

All include the same powerful normalization logic! 🚀

---

## Quick Reference

| Need | Tool | Command |
|------|------|---------|
| Compare directories | FolderComparer | `py main.py --custom-folders --folder1 X --folder2 Y` |
| Compare JAR files | ArchiveContentsComparer | `py compare_crm_jars.py` |
| Web interface | builtin_server | `.\.venv\Scripts\python.exe builtin_server.py --host 0.0.0.0 --port 5000` |
| Test normalization | Test script | `py test_normalization.py` |
| Demo nested paths | Demo script | `py demo_nested_normalization.py` |

**All tools now include normalization by default!** ✨

