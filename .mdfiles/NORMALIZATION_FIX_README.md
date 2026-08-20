# Archive Naming Convention Normalization Fix

## Problem Description

When comparing CRM SDK JAR files between different releases (e.g., HFB22 vs HFB21), the archive comparison tool was reporting "no differences found" even though there were version-specific naming convention differences within nested JAR files, such as:

- `winpack__V10_0_1_22.jar` vs `winpack__V10_0_1_21.jar`
- `SDK_CRM_10.0.1.22_linux64_20251120_2043.jar` vs `SDK_CRM_10.0.1.21_linux64_20250908_1434.jar`
- Unsigned JARs with different naming conventions
- `AmdocsCRM.ear` with embedded version numbers

These differences are expected between different release versions and should be normalized to avoid false positives in the comparison report.

## Solution

Updated the `_normalize_archive_filename()` method in `src/folder_compare.py` to handle CRM-specific naming patterns:

### New Normalization Patterns

1. **CRM Version Patterns**: `__V10_0_1_22` → normalized
   - Matches: `__V` followed by version numbers with underscores or dots
   - Example: `winpack__V10_0_1_22.jar` → `winpack.jar`

2. **Embedded Version Numbers**: `_10.0.1.22_` → normalized
   - Matches: version numbers with dots surrounded by underscores
   - Example: `SDK_CRM_10.0.1.22_linux64.jar` → `SDK_CRM_linux64.jar`

3. **Unsigned JAR Suffix**: `_unsigned` or `-unsigned` → removed
   - Example: `module_unsigned.jar` → `module.jar`

4. **Timestamp Patterns**: `_20251120_2043` → removed
   - Matches: date/time stamps in various formats
   - Example: `file_20251120_2043.jar` → `file.jar`

5. **Standard Version Patterns**: Still supported
   - `-1.2.3`, `_1.2.3-SNAPSHOT`, etc.
   - Example: `lightsaber-10.2.0.3-67024.war` → `lightsaber.war`

## Testing

Run the test script to verify normalization is working correctly:

```bash
py test_normalization.py
```

Expected output: All tests should pass, demonstrating that version-specific naming differences are properly normalized.

## Usage

### Command-Line Comparison

To compare two CRM SDK JAR files with the updated normalization:

```bash
py main.py --custom-folders ^
  --folder1 "\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB22_release\release\SDK_linux\SDK_CRM_10.0.1.22_linux64_20251120_2043.jar" ^
  --folder2 "\\DVCIISILON01\cm_share3\10.0.1\AmdocsCRM10.0.PB1.HFB21_release\release\SDK_linux\SDK_CRM_10.0.1.21_linux64_20250908_1434.jar"
```

### Report Output

The comparison will now:
1. ✅ Normalize version-specific naming differences
2. ✅ Report only meaningful content differences
3. ✅ Ignore expected version number variations
4. ✅ Handle nested JAR/WAR/EAR files recursively

### Example Normalized Matches

The following pairs will now be recognized as identical (after normalization):

| Folder 1 (HFB22) | Folder 2 (HFB21) | Normalized Name |
|------------------|------------------|-----------------|
| `winpack__V10_0_1_22.jar` | `winpack__V10_0_1_21.jar` | `winpack.jar` |
| `SDK_CRM_10.0.1.22_linux64.jar` | `SDK_CRM_10.0.1.21_linux64.jar` | `SDK_CRM_linux64.jar` |
| `module_unsigned.jar` | `module.jar` | `module.jar` |
| `AmdocsCRM_10.0.1.22.ear` | `AmdocsCRM_10.0.1.21.ear` | `AmdocsCRM.ear` |

## Files Modified

- **src/folder_compare.py**: Enhanced `_normalize_archive_filename()` method with CRM-specific patterns
- **test_normalization.py**: New test script to verify normalization behavior

## Benefits

1. ✅ **Reduced False Positives**: Version number differences no longer trigger "different files" alerts
2. ✅ **Better Reporting**: Focus on actual content differences, not naming variations
3. ✅ **Backward Compatible**: All existing normalization patterns still work
4. ✅ **Comprehensive**: Handles nested archives recursively (EAR → WAR → JAR)
5. ✅ **Tested**: Complete test coverage for CRM-specific patterns

## Next Steps

1. Run a new comparison on your JAR files
2. Review the generated report to confirm naming differences are normalized
3. Focus on any remaining content differences (which are actual differences, not naming variations)

## Need Help?

If you encounter any additional naming patterns that aren't being normalized correctly, you can:

1. Add them to the test script (`test_normalization.py`)
2. Update the regex patterns in `_normalize_archive_filename()`
3. Run the tests to verify the fix works

