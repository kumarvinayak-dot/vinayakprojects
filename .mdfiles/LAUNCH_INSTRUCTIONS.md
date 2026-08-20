# Folder Comparison Tool - Launch Instructions

## Overview
This tool provides two ways to compare folders:
1. **Web Interface** - Browser-based UI using Flask (requires Flask installation)
2. **Standalone GUI** - Desktop application using Tkinter (no additional packages required)

---

## Option 1: Web Interface (Browser-Based)

### Prerequisites
- Python 3.x installed
- Flask package installed (see Flask Installation section below if not installed)

### How to Start the Web Server

#### Method A: Using the Batch Script (Recommended)
1. Double-click `start_server.bat` in the project folder
2. The server will start and display:
   ```
   Starting Folder Comparison Web Server
   Server will start at: http://127.0.0.1:5000
   ```
3. Open your web browser and navigate to: **http://127.0.0.1:5000**
4. Use the web interface to configure and run comparisons

#### Method B: Using Command Line
1. Open Command Prompt or PowerShell
2. Navigate to the project folder:
   ```powershell
   cd C:\Users\SVINAYAK\python_project
   ```
3. Run the Flask server:
   ```powershell
   py web_app.py
   ```
   OR
   ```powershell
   $env:FLASK_APP="web_app.py"
   $env:FLASK_ENV="development"
   py -m flask run --host=127.0.0.1 --port=5000
   ```
4. Open your browser to: **http://127.0.0.1:5000**

### Using the Web Interface
1. Enter **Folder 1 Path** (e.g., `\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\...`)
2. Enter **Folder 2 Path**
3. Select **Comparison Mode**:
   - Full Content Comparison
   - Naming Convention
   - Archives Comparison
4. Configure options:
   - ☑ Copy network folders locally (recommended for network paths)
   - Set max retries and retry delay if needed
5. Click **Start Comparison**
6. View results in the browser or click links to download reports

### Stopping the Web Server
- Press `Ctrl+C` in the terminal/command window where the server is running

---

## Option 2: Standalone GUI (Desktop Application)

### Prerequisites
- Python 3.x installed
- Tkinter (included with standard Python installation)

### How to Start the Standalone GUI

#### Method A: Using the Batch Script (Recommended)
1. Double-click `start_gui.bat` in the project folder
2. The GUI window will open automatically

#### Method B: Using Command Line
1. Open Command Prompt or PowerShell
2. Navigate to the project folder:
   ```powershell
   cd C:\Users\SVINAYAK\python_project
   ```
3. Run the GUI application:
   ```powershell
   py compare_folders_gui.py
   ```

### Using the Standalone GUI
1. Enter **Folder 1 Path** or click **Browse** to select
2. Enter **Folder 2 Path** or click **Browse** to select
3. Select **Comparison Mode** from the dropdown:
   - Full Content Comparison
   - Naming Convention Comparison
   - Archives Comparison
4. Configure options:
   - ☑ Copy network folders to local (recommended for network paths)
   - Set max retries and retry delay
5. Click **Start Comparison**
6. Monitor progress in the log window
7. When complete, click **Open Report** to view the HTML report

### Closing the Standalone GUI
- Click the **X** button on the window
- Or select **File → Exit** (if menu is available)

---

## Flask Installation

### If Flask is Already Installed
- Skip this section and use the Web Interface directly

### If Flask is NOT Installed

#### Option 1: Install from Internet (Requires Internet Connection)
```powershell
py -m pip install flask
```

#### Option 2: Offline Installation (No Internet Required)
If you cannot connect to the internet on this machine:

1. **On a machine WITH internet:**
   - Download Flask wheel files:
     ```bash
     pip download flask -d flask_wheels
     ```
   - This will download Flask and all its dependencies

2. **Transfer files to this machine:**
   - Copy the entire `flask_wheels` folder to this machine

3. **On this machine (without internet):**
   ```powershell
   py -m pip install --no-index --find-links=flask_wheels flask
   ```

See `INSTALL_FLASK_OFFLINE.md` for detailed offline installation instructions.

---

## Comparison Modes Explained

### 1. Full Content Comparison
- Compares all files in both folders
- Checks file content using MD5 hashes
- Identifies:
  - Exact matches (same content)
  - Modified files (different content)
  - Missing files (in one folder but not the other)
- **Best for:** Complete folder synchronization checks

### 2. Naming Convention Comparison
- Focuses on file naming patterns
- Identifies:
  - Exact name matches
  - Similar filenames (potential naming inconsistencies)
  - Missing files
- **Best for:** Verifying naming standards across releases

### 3. Archives Comparison
- Compares contents within archive files (JAR, WAR, EAR, ZIP, RPM)
- Checks:
  - Archive members/contents
  - File counts within archives
  - Missing or extra files in archives
- **Best for:** Comparing SDK releases, deployment packages

---

## Network Path Handling

### Recommended Settings for Network Paths
When comparing folders on network shares (e.g., `\\SERVER\share\path`):

1. ☑ **Enable "Copy network folders locally"**
   - This copies network folders to local temp storage before comparison
   - Significantly improves performance and reliability
   - Reduces network timeouts and connection issues

2. **Set appropriate retry settings:**
   - Max Retries: 3 (default)
   - Retry Delay: 1 second (default)

### Network Path Examples
```
\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\SDK
\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06
```

---

## Output Files and Reports

### Report Locations
Reports are saved in the `reports/` folder by default:
- `reports/comparison_report_YYYYMMDD_HHMMSS.txt` (text format)
- `reports/comparison_report_YYYYMMDD_HHMMSS.html` (HTML format)

### Report Types
- **Full Comparison Report:** Lists all files with match status, MD5 hashes, sizes
- **Naming Convention Report:** Grouped by exact matches, missing files, potential mismatches
- **Archives Report:** Archive-by-archive comparison with member lists

### Viewing Reports
- **Web Interface:** Reports open automatically in a new browser tab
- **Standalone GUI:** Click "Open Report" button after comparison completes
- **Manual:** Navigate to `reports/` folder and open the HTML file in any browser

---

## Troubleshooting

### Web Interface Issues

**Problem:** `No module named flask`
- **Solution:** Install Flask (see Flask Installation section above)

**Problem:** Port 5000 is already in use
- **Solution:** Stop other processes using port 5000, or modify `web_app.py` to use a different port

**Problem:** Page gets stuck / comparison never completes
- **Solution:** 
  - Check the terminal window for error messages
  - For large folders or slow network paths, be patient (may take several minutes)
  - Consider using "Copy network folders locally" option

### Standalone GUI Issues

**Problem:** GUI window doesn't open
- **Solution:** 
  - Check that Python is installed correctly
  - Try running from command line to see error messages: `py compare_folders_gui.py`

**Problem:** "tkinter not found"
- **Solution:** Reinstall Python with tkinter support enabled

### General Issues

**Problem:** Network path access denied
- **Solution:** 
  - Verify you have read permissions on both network paths
  - Try mapping the network drive and using the mapped drive letter

**Problem:** Comparison takes too long
- **Solution:**
  - Enable "Copy network folders locally" for network paths
  - Use more specific folder paths (avoid comparing very large folder trees)

**Problem:** Out of memory errors
- **Solution:**
  - Use more specific folder paths
  - Close other applications to free up memory
  - For very large comparisons, use the command-line tool: `run_comparison_standalone.py`

---

## Quick Start Examples

### Example 1: Compare Two Network SDK Folders (Web Interface)
1. Start web server: double-click `start_server.bat`
2. Open browser to http://127.0.0.1:5000
3. Enter:
   - Folder 1: `\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\SDK1`
   - Folder 2: `\\DVCIISILON01\cm_share4\CRM9_SP22\9.22.1\SDK2`
   - Mode: Full Content Comparison
   - ☑ Copy network folders locally
4. Click "Start Comparison"
5. Wait for completion and view report

### Example 2: Compare Naming Conventions (Standalone GUI)
1. Start GUI: double-click `start_gui.bat`
2. Enter:
   - Folder 1: `\\SERVER\path\to\folder1`
   - Folder 2: `\\SERVER\path\to\folder2`
   - Mode: Naming Convention Comparison
   - ☑ Copy network folders to local
3. Click "Start Comparison"
4. Monitor progress in log window
5. Click "Open Report" when done

### Example 3: Compare Archive Contents (Command Line)
```powershell
py run_comparison_standalone.py
# Follow prompts to enter paths and select mode
```

---

## Additional Resources

- **Flask Installation:** See `INSTALL_FLASK_OFFLINE.md`
- **Requirements:** See `requirements.txt`
- **Source Code:** See `src/folder_compare.py`
- **Fix History:** See `FIX_SUMMARY.md`

---

## Support

For issues or questions:
1. Check the log output in the terminal/GUI window
2. Review the troubleshooting section above
3. Check that all prerequisites are installed
4. Verify folder paths are accessible

---

**Last Updated:** November 27, 2025
**Version:** 2.0 (with bug fixes for naming convention comparison)











