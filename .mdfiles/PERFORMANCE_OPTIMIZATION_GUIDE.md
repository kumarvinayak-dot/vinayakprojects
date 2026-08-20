# Performance Optimization Guide

## Issue

After integrating all comparison logics, the full content comparison can be slow because it now performs deep archive contents comparison (with normalization) on EVERY archive file (.jar/.war/.ear/.zip/.rpm).

For directories with many archives, this can take significant time.

---

## Solutions

### Option 1: Fast Mode - Skip Deep Archive Comparison (Fastest)

Use this when you only need file-level comparison without checking archive contents:

```bash
py main.py --custom-folders \
  --folder1 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06" \
  --folder2 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07" \
  --no-deep-archive-compare \
  --show-all-differences
```

**Performance**: ✅ **FASTEST** - Skips all archive contents analysis
**Use when**: You only need to know which files are different, not what's inside archives

---

### Option 2: Smart Mode - Deep Compare Only If Different (Recommended)

Only performs deep archive analysis when archive files differ at the file level:

```bash
py main.py --custom-folders \
  --folder1 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06" \
  --folder2 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07" \
  --deep-compare-if-different \
  --show-all-differences
```

**Performance**: ✅ **FAST** - Only analyzes archives that already differ
**Use when**: Most archives are identical; you want deep analysis only for changed ones

---

### Option 3: Full Mode - Complete Analysis (Slowest)

Default behavior - performs deep archive analysis on all archives:

```bash
py main.py --custom-folders \
  --folder1 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06" \
  --folder2 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07" \
  --show-all-differences
```

**Performance**: ⚠️ **SLOWER** - Analyzes ALL archives even if identical
**Use when**: You need comprehensive archive analysis for all files

---

## Performance Comparison

| Mode | Time Estimate* | Archives Analyzed | Best For |
|------|---------------|-------------------|----------|
| **Fast** (`--no-deep-archive-compare`) | Minutes | 0 | Quick file-level comparison |
| **Smart** (`--deep-compare-if-different`) | 10-30 min | Only changed | Most use cases |
| **Full** (default) | 30-60+ min | All archives | Complete analysis |

*Time estimates for directories with 1000+ archive files

---

## Progress Tracking

By default, the tool shows progress:

```
INFO - Found 2453 files in folder 1, 2451 files in folder 2
INFO - Total files to compare: 2503
INFO - Progress: 100/2503 files processed (4.0%)
INFO - Deep archive comparison [1]: path/to/archive.jar
INFO - Progress: 200/2503 files processed (8.0%)
...
```

To disable progress logging:

```bash
py main.py --custom-folders \
  --folder1 "path1" \
  --folder2 "path2" \
  --no-progress
```

---

## Understanding What Takes Time

### Fast Operations:
- ✅ File existence check
- ✅ File size comparison
- ✅ Timestamp comparison
- ✅ File hash calculation (for different sizes)

### Slow Operations:
- ⏱️ Archive member listing (especially with Java jar fallback)
- ⏱️ Member name normalization (3700+ members)
- ⏱️ Nested archive recursion (EAR → WAR → JAR)

---

## Recommended Workflows

### Scenario 1: Quick Check
**Goal**: See which files are different

```bash
py main.py --custom-folders \
  --folder1 "path1" \
  --folder2 "path2" \
  --no-deep-archive-compare \
  --show-all-differences
```

**Time**: 2-5 minutes  
**Output**: List of different files (including archives that differ)

---

### Scenario 2: Smart Analysis (Recommended)
**Goal**: Full analysis, but skip archives that are identical

```bash
py main.py --custom-folders \
  --folder1 "path1" \
  --folder2 "path2" \
  --deep-compare-if-different \
  --show-all-differences
```

**Time**: 10-20 minutes  
**Output**: Full report with deep analysis for changed archives only

---

### Scenario 3: Complete Analysis
**Goal**: Analyze every archive in detail (even if identical)

```bash
py main.py --custom-folders \
  --folder1 "path1" \
  --folder2 "path2" \
  --show-all-differences
```

**Time**: 30-60+ minutes  
**Output**: Complete comprehensive report with all archive details

---

## Python API

For programmatic use:

```python
from folder_compare import FolderComparer

comparer = FolderComparer(folder1, folder2)

# Fast mode
comparer.compare_archive_contents = False

# Smart mode (recommended)
comparer.compare_archive_contents = True
comparer.compare_archive_contents_only_if_different = True

# Full mode (default)
comparer.compare_archive_contents = True
comparer.compare_archive_contents_only_if_different = False

# Control progress
comparer.show_progress = True  # or False

results = comparer.compare_folders()
```

---

## Troubleshooting

### Comparison Appears Stuck

**Check progress logs** - The tool shows which file it's processing:
```
INFO - Deep archive comparison [23]: path/to/large.jar
```

If stuck on one file for >5 minutes, it may be:
- Very large archive (>100MB)
- Corrupt archive requiring Java jar fallback
- Network issues if on network path

**Solution**: Use `--deep-compare-if-different` or `--no-deep-archive-compare`

---

### Still Too Slow?

**Try these optimizations:**

1. **Use local copies** (for network paths):
```bash
py main.py --custom-folders \
  --folder1 "path1" \
  --folder2 "path2" \
  --use-local-copy \
  --deep-compare-if-different
```

2. **Run in stages**:
```bash
# Stage 1: Quick check (fast)
py main.py --no-deep-archive-compare ...

# Stage 2: Deep dive on specific archives
py compare_crm_jars.py  # For specific JARs
```

---

## Summary Table

| Flag | Effect | Performance Impact |
|------|--------|-------------------|
| `--no-deep-archive-compare` | Skip all archive analysis | ✅ **Fastest** |
| `--deep-compare-if-different` | Analyze only changed archives | ✅ **Fast** |
| (default) | Analyze all archives | ⚠️ **Slower** |
| `--no-progress` | Disable progress logging | Minimal |
| `--use-local-copy` | Copy to local first | Initial copy time, then faster |

---

## Recommendation for Your Case

For comparing `ASCF_HF_10240pb01hf06` vs `ASCF_HF_10240pb01hf07`:

```bash
# Recommended: Smart mode with progress
py main.py --custom-folders \
  --folder1 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06" \
  --folder2 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07" \
  --deep-compare-if-different \
  --show-all-differences
```

This will:
- ✅ Compare all files quickly
- ✅ Do deep analysis only on archives that differ
- ✅ Show progress so you know it's working
- ✅ Complete in 10-20 minutes (vs 60+ minutes in full mode)

---

## Need Help?

- Progress stopped? Check the last log message to see which file it's on
- Taking too long? Use `--deep-compare-if-different` or `--no-deep-archive-compare`
- Want to see what's happening? Progress is enabled by default
- Need fastest results? Use `--no-deep-archive-compare` for file-level only

---

**Key Takeaway**: Use `--deep-compare-if-different` for the best balance of speed and completeness! 🚀

