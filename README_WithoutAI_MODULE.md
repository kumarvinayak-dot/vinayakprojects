# Folder Comparison Tool

A comprehensive Python-based tool for comparing folders, with special support for network paths and archive files (JAR, WAR, EAR, ZIP, RPM).

## Features

- **Three Comparison Modes:**
  - 📁 Full Content Comparison - Complete file-by-file comparison with MD5 hashing
  - 📝 Naming Convention - Analyze filename patterns and naming consistency
  - 📦 Archives Comparison - Deep comparison of archive file contents

- **Network Path Support:**
  - Handles UNC paths (`\\SERVER\share\path`)
  - Optional local copying for improved performance
  - Automatic retry on network failures

- **Flexible Interfaces:**
  - 🌐 Web-based UI (Flask)
  - 🖥️ Desktop GUI (Tkinter)
  - 💻 Command-line tool

- **Comprehensive Reports:**
  - HTML reports with color-coded results
  - Text reports for documentation
  - Automatic browser opening
  - Detailed statistics and insights

## Quick Start

### Web Interface (Recommended for First-Time Users)
```bash
# Start the web server
start_server.bat

# Open in browser
http://127.0.0.1:5000
```

### Standalone GUI (No Flask Required)
```bash
# Start the GUI application
start_gui.bat
```

### Command Line
```bash
# Run standalone comparison
py run_comparison_standalone.py
```

## Installation

### Prerequisites
- Python 3.7 or higher
- Tkinter (usually included with Python)

### Optional Dependencies
For the web interface:
```bash
py -m pip install flask
```

Or see `INSTALL_FLASK_OFFLINE.md` for offline installation instructions.

### Required Packages
```
flask  # Only for web interface
```

All other dependencies are part of Python standard library.

## Project Structure

```
python_project/
├── src/
│   └── folder_compare.py        # Core comparison logic
├── web_app.py                    # Flask web application
├── compare_folders_gui.py        # Tkinter GUI application
├── run_comparison_standalone.py  # Command-line tool
├── start_server.bat              # Launch web server (Windows)
├── start_gui.bat                 # Launch GUI (Windows)
├── reports/                      # Generated comparison reports
├── LAUNCH_INSTRUCTIONS.md        # Detailed launch instructions
├── QUICK_START.md                # Quick reference guide
├── INSTALL_FLASK_OFFLINE.md      # Offline Flask installation
├── FIX_SUMMARY.md                # Recent bug fixes
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

## Usage Examples

### Example 1: Compare Two SDK Folders
```python
# Using the GUI or Web Interface:
Folder 1: \\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\SDK1
Folder 2: \\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\SDK2
Mode: Full Content Comparison
☑ Copy network folders locally
```

### Example 2: Check Naming Conventions
```python
# Using the GUI or Web Interface:
Folder 1: \\SERVER\release\v1.0
Folder 2: \\SERVER\release\v2.0
Mode: Naming Convention
☑ Copy network folders locally
```

### Example 3: Compare Archive Contents
```python
# Using the GUI or Web Interface:
Folder 1: C:\builds\app-v1
Folder 2: C:\builds\app-v2
Mode: Archives Comparison
```

## Comparison Modes

### 1. Full Content Comparison
Performs a complete comparison of all files:
- ✅ Exact matches (identical content)
- ⚠️ Modified files (different content, same name)
- ❌ Missing files (exist in one folder only)
- 📊 File sizes and MD5 hashes
- 📈 Statistical summary

**Use When:** You need to verify complete folder synchronization or identify any differences.

### 2. Naming Convention
Analyzes file naming patterns:
- ✅ Exact name matches
- 🔍 Similar filenames (potential naming issues)
- ❌ Missing files
- 📋 Duplicate filenames in different paths
- 💡 Similarity scoring

**Use When:** You want to ensure naming standards are consistent across releases.

### 3. Archives Comparison
Deep comparison of archive contents:
- 📦 Compares JAR, WAR, EAR, ZIP, RPM files
- 📝 Lists all members within archives
- ✅ Exact member matches
- ❌ Missing or extra members
- 📊 Member counts and statistics

**Use When:** You need to compare SDK releases, deployment packages, or any archive-based distributions.

## Network Path Handling

### Why Local Copying Helps
When comparing folders on network shares:
- ✅ Significantly faster (local disk vs. network)
- ✅ More reliable (no network timeouts)
- ✅ Better for large folders
- ✅ Handles slow or unstable networks

### How It Works
1. Tool creates temporary local folders
2. Copies network folders to local disk
3. Performs comparison on local copies
4. Cleans up temporary folders after completion

### Settings
```
☑ Copy network folders locally (recommended)
Max Retries: 3
Retry Delay: 1 second
```

## Reports

### Report Types
Reports are generated in both text and HTML formats:

**Text Report (.txt)**
- Plain text format
- Easy to share and archive
- Searchable

**HTML Report (.html)**
- Color-coded results
- Expandable sections
- Interactive
- Opens automatically in browser

### Report Location
```
reports/
├── comparison_report_20251127_120000.txt
└── comparison_report_20251127_120000.html
```

### Report Sections
- **Summary Statistics:** Overall counts and percentages
- **Exact Matches:** Files that are identical
- **Modified Files:** Files with different content
- **Missing Files:** Files in one folder but not the other
- **Analysis Insights:** Recommendations and warnings
- **Duplicate Detection:** Files with same name in different paths

## Troubleshooting

### Common Issues

**"No module named flask"**
- Install Flask: `py -m pip install flask`
- Or use Standalone GUI (no Flask required)

**"Access denied" on network paths**
- Verify you have read permissions
- Try mapping the network drive
- Use local copies of folders

**Comparison takes too long**
- Enable "Copy network folders locally"
- Use more specific folder paths
- Check network connection speed

**Out of memory**
- Compare smaller folder subsets
- Close other applications
- Increase virtual memory

### Getting Help
1. Check `LAUNCH_INSTRUCTIONS.md` for detailed guidance
2. Review log output in terminal/GUI window
3. Check `FIX_SUMMARY.md` for recent fixes
4. Verify all paths are accessible

## Recent Updates

### Version 2.0 (November 2025)
- ✅ Fixed naming convention comparison error
- ✅ Improved error handling and reporting
- ✅ Added network path retry logic
- ✅ Enhanced duplicate filename detection
- ✅ Better progress feedback
- ✅ Comprehensive HTML reports

See `FIX_SUMMARY.md` for complete details.

## Documentation

- **📖 LAUNCH_INSTRUCTIONS.md** - Complete launch and usage guide
- **⚡ QUICK_START.md** - Quick reference card
- **💾 INSTALL_FLASK_OFFLINE.md** - Offline Flask installation
- **🔧 FIX_SUMMARY.md** - Recent bug fixes and changes

## System Requirements

- **Operating System:** Windows (tested on Windows Server 2019/2022)
- **Python:** 3.7 or higher
- **Memory:** 4GB RAM minimum (8GB+ recommended for large comparisons)
- **Disk Space:** Sufficient for temporary copies if using network path copying
- **Network:** Access to network shares (if comparing network paths)

## Performance

### Typical Comparison Times
- Small folders (< 100 files): < 1 minute
- Medium folders (100-1000 files): 1-5 minutes
- Large folders (1000+ files): 5-30 minutes
- Very large folders (10,000+ files): 30+ minutes

*Times vary based on file sizes, network speed, and whether local copying is enabled.*

## License

This tool is provided as-is for internal use.

## Support

For issues, questions, or feature requests:
1. Check the documentation in this folder
2. Review troubleshooting section above
3. Check log output for error details
4. Verify system requirements are met

## Developer Notes

### Core Module
- `src/folder_compare.py` - Main comparison logic
  - `FolderComparer` - Full content comparison
  - `NamingConventionComparer` - Naming analysis
  - `ArchiveContentsComparer` - Archive comparison

### Interfaces
- `web_app.py` - Flask web application
- `compare_folders_gui.py` - Tkinter GUI
- `run_comparison_standalone.py` - CLI tool

### Testing
Run the test comparison:
```bash
py test_naming_fix.py
```

---

**Version:** 2.0  
**Last Updated:** November 27, 2025  
**Status:** ✅ All features working











