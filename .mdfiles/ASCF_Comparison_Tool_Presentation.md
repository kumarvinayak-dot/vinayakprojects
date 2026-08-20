# ASCF Patch Comparison Tool
## Complete User Guide & Technical Documentation

---

# 📋 Table of Contents

1. [Tool Overview](#slide-1-tool-overview)
2. [Key Features](#slide-2-key-features)
3. [Comparison Modes](#slide-3-comparison-modes)
4. [Full Content Comparison](#slide-4-full-content-comparison)
5. [JAR Naming Convention](#slide-5-jar-naming-convention)
6. [Archive Contents Comparison](#slide-6-archive-contents-comparison)
7. [RPM CPIO Comparison](#slide-7-rpm-cpio-comparison)
8. [FOSS Foundation](#slide-8-foss-foundation)
9. [Copy Mode](#slide-9-copy-mode)
10. [Technical Architecture](#slide-10-technical-architecture)
11. [Tools & Technologies](#slide-11-tools-technologies)
12. [Server Management](#slide-12-server-management)
13. [Cache Management](#slide-13-cache-management)
14. [Debugging Guide](#slide-14-debugging-guide)
15. [Multi-User Support](#slide-15-multi-user-support)
16. [Setup Instructions](#slide-16-setup-instructions)

---

# Slide 1: Tool Overview

## ASCF Patch Comparison Tool

### Purpose
A web-based tool for comparing software builds, patches, and archives to identify:
- 📁 New, modified, and removed files
- 📊 Size changes and content differences
- 🔄 Version changes in JAR files
- ☕ Decompiled Java class differences

### Access URL
```
http://<server-ip>:5000
Example: http://192.168.1.41:5000
```

### Supported Platforms
- Windows Server / Windows 10/11
- Linux (with Python 3.8+)
- Network share paths (UNC paths)

---

# Slide 2: Key Features

## ✨ Feature Highlights

| Feature | Description |
|---------|-------------|
| 🔍 **Multi-Mode Comparison** | 4 different comparison modes for various use cases |
| 📦 **Nested Archive Support** | Compares inside EAR → WAR → JAR → Class files |
| 📦 **RPM CPIO Support** | Extracts and compares RPM packages |
| ☕ **Java Decompilation** | Uses CFR to show Java source differences |
| 📋 **Copy with Progress** | Copy files with real-time progress bar |
| 📊 **HTML Reports** | Beautiful, interactive reports with navigation |
| 👥 **Multi-User Support** | Concurrent users with thread-safe operations |
| 🔄 **Progress Tracking** | Real-time progress with ETA |

---

# Slide 3: Comparison Modes

## 🔄 Available Comparison Modes

```
┌─────────────────────────────────────────────────────────────┐
│                    COMPARISON MODES                          │
├─────────────────┬───────────────────────────────────────────┤
│ Full Content    │ Complete file-by-file comparison          │
│ Comparison      │ Shows all differences with details        │
├─────────────────┼───────────────────────────────────────────┤
│ JAR Naming      │ Compares JAR file naming conventions      │
│ Convention      │ Identifies version changes                │
├─────────────────┼───────────────────────────────────────────┤
│ Archives        │ Deep comparison of EAR/WAR/JAR/ZIP/RPM    │
│ Contents        │ With nested archive support               │
├─────────────────┼───────────────────────────────────────────┤
│ FOSS Foundation │ Compares specific JAR versions            │
│                 │ (MANIFEST.MF analysis)                    │
└─────────────────┴───────────────────────────────────────────┘
```

---

# Slide 4: Full Content Comparison

## 📁 Full Content Comparison Mode

### What It Does
- Compares every file in two directories
- Identifies: Added, Removed, Modified, Unchanged files
- Shows size differences with percentages
- Color-coded results (Red = grew, Green = shrunk)

### Logic Implemented
```python
1. Walk through both directories
2. Build file inventory with:
   - File path (relative)
   - File size
   - Content hash (CRC32/MD5)
3. Compare inventories:
   - Only in Old → REMOVED
   - Only in New → ADDED
   - In Both, different hash → MODIFIED
   - In Both, same hash → UNCHANGED
```

### Report Sections
- 📊 Summary Statistics
- ❌ Removed Files
- ✅ Added Files
- ⚠️ Modified Files (with size change)
- ✓ Unchanged Files

---

# Slide 5: JAR Naming Convention

## 📦 JAR Naming Convention Mode

### What It Does
- Compares JAR file names between builds
- Identifies version pattern changes
- Detects renamed JARs
- Shows size differences

### Naming Pattern Detection
```
Example JAR names analyzed:
- commons-logging-1.2.jar → commons-logging-1.3.jar (Version change)
- log4j-core-2.14.1.jar → log4j-core-2.17.1.jar (Security update)
- myapp_10.25.0.pb00.jar → myapp_10.25.0.pb00_hf01.jar (Hotfix)
```

### Logic Implemented
```python
1. Extract base name (remove version numbers)
2. Group JARs by base name
3. Compare versions:
   - Same base, different version → VERSION CHANGE
   - Only in Old → REMOVED
   - Only in New → ADDED
4. Calculate size differences
```

---

# Slide 6: Archive Contents Comparison

## 📦 Archive Contents Comparison Mode

### Supported Archive Types
| Type | Extension | Notes |
|------|-----------|-------|
| Enterprise Archive | .ear | Contains WAR/JAR |
| Web Archive | .war | Contains classes/JARs |
| Java Archive | .jar | Contains classes |
| ZIP Archive | .zip | Generic archive |
| RPM Package | .rpm | CPIO-based Linux package |

### Nested Comparison Flow
```
EAR File
  └── WAR File
       └── WEB-INF/lib/
            └── JAR Files
                 └── .class Files
                      └── Decompiled Java Source
```

### Logic Implemented
```python
1. Detect archive type by extension
2. For each archive:
   a. Open and read member list
   b. For nested archives (WAR/JAR inside EAR):
      - Recursively scan contents
      - Track parent archive path
   c. Calculate member hash/size
3. Compare members across archives
4. Generate detailed report with:
   - JAR size differences
   - Version changes
   - Content changes (with View Diff button)
```

---

# Slide 7: RPM CPIO Comparison

## 📦 RPM CPIO Structure Comparison

### RPM File Structure
```
┌─────────────────────────────────────┐
│         RPM Package File            │
├─────────────────────────────────────┤
│ Lead (96 bytes)                     │
│ Signature Header                    │
│ Header                              │
│ ┌─────────────────────────────────┐ │
│ │   CPIO Archive (gzip)           │ │
│ │   ├── ./opt/app/ASCFClient.ear  │ │
│ │   │    └── ASCF.war             │ │
│ │   │         └── WEB-INF/lib/    │ │
│ │   │              └── *.jar      │ │
│ │   └── ./opt/config/*.xml        │ │
│ └─────────────────────────────────┘ │
└─────────────────────────────────────┘
```

### RPM Parsing Logic
```python
1. Find gzip signature (0x1f 0x8b) in RPM
2. Decompress CPIO data
3. Parse CPIO newc format:
   - Magic: 070701 or 070702
   - Parse header fields (filesize, namesize)
   - Extract file content
4. For nested archives (EAR/WAR/JAR):
   - Recursively scan using ZIP library
5. Compare all extracted members
```

### Report Shows
- 📊 RPM Summary Stats
- ⚠️ Content Changes Table
- ✅ New Files in RPM
- ❌ Removed Files from RPM
- 📁 Structure Tree View

---

# Slide 8: FOSS Foundation

## 🔍 FOSS Foundation Mode

### What It Does
- Compares specific JAR file versions
- Analyzes MANIFEST.MF files
- Useful for FOSS compliance checks

### MANIFEST.MF Analysis
```
Manifest-Version: 1.0
Bundle-Name: Commons Logging
Bundle-SymbolicName: org.apache.commons.logging
Bundle-Version: 1.2.0
Implementation-Version: 1.2
```

### Logic Implemented
```python
1. Locate specified JAR file
2. Extract MANIFEST.MF
3. Parse version attributes:
   - Implementation-Version
   - Bundle-Version
   - Specification-Version
4. Compare versions between builds
5. Report version differences
```

---

# Slide 9: Copy Mode

## 📋 Copy Mode with Progress Tracking

### Features
- ✅ Copy files/folders with real-time progress
- 📊 Shows bytes copied, files processed
- ⏱️ Estimated time remaining
- ⚠️ Graceful error handling (skips inaccessible files)
- 📝 Reports skipped files with reasons

### Progress UI
```
┌─────────────────────────────────────────────────┐
│ 📋 Copy in Progress                             │
│ ████████████████░░░░░░░░░░░░░░░░ 52%           │
│                                                 │
│ Status: Copying...                              │
│ Files: 156 / 300                                │
│ Size: 45.2 MB / 87.1 MB                        │
│ Current: WEB-INF/lib/commons-io.jar            │
│ ETA: 2 minutes 15 seconds                       │
│                                                 │
│ ⚠️ Skipped: 3 files (Access Denied)            │
└─────────────────────────────────────────────────┘
```

### Copy Logic
```python
1. Initialize progress tracking
2. Scan source directory:
   - Count total files
   - Calculate total bytes
3. For each file:
   - Update current file name
   - Create destination directories
   - Copy file with shutil.copy2
   - Handle errors gracefully
   - Update progress/ETA
4. Report completion with summary
```

---

# Slide 10: Technical Architecture

## 🏗️ Technical Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Web Browser                             │
│                   (HTML5 + JavaScript)                       │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────────┐
│               Built-in HTTP Server                           │
│              (ThreadingHTTPServer)                           │
│  ┌─────────────┬─────────────┬─────────────┬──────────────┐ │
│  │   do_GET    │   do_POST   │  /progress  │ /copy_async  │ │
│  │ (pages/run) │   (copy)    │  (polling)  │ (background) │ │
│  └─────────────┴─────────────┴─────────────┴──────────────┘ │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                   Comparison Engine                          │
│  ┌──────────────────┐  ┌──────────────────┐                │
│  │  FolderComparer  │  │ArchiveContents   │                │
│  │                  │  │   Comparer       │                │
│  └──────────────────┘  └──────────────────┘                │
│  ┌──────────────────┐  ┌──────────────────┐                │
│  │NamingConvention  │  │ JarManifest      │                │
│  │   Comparer       │  │  Comparer        │                │
│  └──────────────────┘  └──────────────────┘                │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                   File System / Network                      │
│        (Local Paths, UNC Paths, Archive Files)              │
└─────────────────────────────────────────────────────────────┘
```

---

# Slide 11: Tools & Technologies

## 🛠️ Tools & Technologies Used

### Core Technologies
| Component | Technology | Purpose |
|-----------|------------|---------|
| Backend | Python 3.8+ | Server logic |
| Web Server | http.server (ThreadingHTTPServer) | HTTP handling |
| Frontend | HTML5 + CSS3 + JavaScript | User interface |
| Archive Processing | zipfile (built-in) | ZIP/JAR/WAR/EAR |
| RPM Processing | gzip + manual CPIO parser | RPM extraction |

### Java Decompilation
| Tool | Purpose | Configuration |
|------|---------|---------------|
| CFR | Decompile .class to .java | Set CFR_JAR environment variable |
| javap | Fallback disassembler | Java JDK required |

### Libraries Used
```python
# Standard Library
import os, sys, time, socket, threading, subprocess
import zipfile, gzip, hashlib, html, json
from pathlib import Path
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
from io import BytesIO

# Optional
import difflib  # HTML diff generation
import re       # Pattern matching
import shutil   # File operations
```

---

# Slide 12: Server Management

## 🖥️ Server Start/Stop Instructions

### Starting the Server
```powershell
# Basic start
cd C:\PythonProject_UPDATED\python_project
python builtin_server.py --host 0.0.0.0 --port 5000

# With CFR decompiler support
$env:CFR_JAR = "C:\cfr-0.152.jar"
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo

# Start with specific options
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

### Stopping the Server
```powershell
# Method 1: Ctrl+C in terminal
Press Ctrl+C

# Method 2: Find and kill by port
netstat -ano | findstr ":5000" | findstr "LISTENING"
taskkill /F /PID <process_id>

# Method 3: Kill all Python processes
Get-Process -Name python | Stop-Process -Force
```

### Command-Line Options
| Option | Description |
|--------|-------------|
| --host | IP to bind (0.0.0.0 for all interfaces) |
| --port | Port number (default: 5000) |
| --skip-demo | Skip startup demos |

---

# Slide 13: Cache Management

## 🗑️ Cache Clearing Instructions

### Why Clear Cache?
- Code changes not reflecting
- Stale comparison results
- Import errors after updates

### Clear Python Cache
```powershell
# Clear __pycache__ directories
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force src\__pycache__ -ErrorAction SilentlyContinue

# Clear .pyc files
Get-ChildItem -Path . -Filter *.pyc -Recurse | Remove-Item -Force
```

### Full Server Restart with Cache Clear
```powershell
# Complete restart sequence
$env:CFR_JAR = "C:\cfr-0.152.jar"

# Kill existing server
netstat -ano | findstr ":5000" | findstr "LISTENING"
taskkill /F /PID <pid>

# Clear cache
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force src\__pycache__ -ErrorAction SilentlyContinue

# Wait and restart
Start-Sleep -Seconds 2
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

### One-Liner for Full Restart
```powershell
taskkill /F /PID $(netstat -ano | findstr ":5000" | findstr "LISTENING" | ForEach-Object { $_.Split()[-1] }) 2>$null; Remove-Item -Recurse -Force __pycache__,src\__pycache__ -ErrorAction SilentlyContinue; Start-Sleep 2; $env:CFR_JAR="C:\cfr-0.152.jar"; python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

---

# Slide 14: Debugging Guide

## 🔧 Debugging Instructions

### Common Issues & Solutions

#### 1. "Access Denied" Errors
```
Error: [WinError 5] Access is denied
```
**Solutions:**
- Run PowerShell as Administrator
- Check network share permissions
- Navigate to the share in Explorer first to authenticate
- Use `net use \\server\share /user:username` to authenticate

#### 2. "No comparison results available"
**Solutions:**
- Verify paths exist and are accessible
- Check for typos in UNC paths
- Try local paths first to test
- Check server console for detailed errors

#### 3. "Module not found" Errors
```powershell
# Reinstall dependencies
pip install -r requirements.txt --force-reinstall
```

#### 4. Progress Bar Not Updating
**Solutions:**
- Clear browser cache (Ctrl+Shift+R)
- Check browser console for JavaScript errors (F12)
- Restart server with cache clear

#### 5. Java Decompilation Not Working
```powershell
# Verify CFR setup
echo $env:CFR_JAR  # Should show path to cfr.jar
java -version      # Java must be installed

# Test CFR manually
java -jar C:\cfr-0.152.jar --help
```

### Debug Mode
```python
# Add debug prints in builtin_server.py
print(f"[DEBUG] Variable: {variable}")
```

### Check Server Logs
```powershell
# Server output shows all requests and errors
# Look for lines starting with [DEBUG] or ERROR
```

---

# Slide 15: Multi-User Support

## 👥 Multi-User Support

### Architecture
```
┌─────────────────────────────────────────────────────────────┐
│                  ThreadingHTTPServer                         │
│           (Handles concurrent requests)                      │
├───────────────┬───────────────┬───────────────┬─────────────┤
│   User 1      │    User 2     │    User 3     │   User N    │
│  Thread 1     │   Thread 2    │   Thread 3    │  Thread N   │
├───────────────┴───────────────┴───────────────┴─────────────┤
│              Shared Progress Tracking                        │
│    comparison_progress = { running, progress, total, ... }  │
├─────────────────────────────────────────────────────────────┤
│              File System (Thread-Safe Read)                  │
└─────────────────────────────────────────────────────────────┘
```

### Concurrent User Limits
| Resource | Limit | Notes |
|----------|-------|-------|
| Concurrent Connections | ~100 | ThreadingHTTPServer default |
| Simultaneous Comparisons | 1 | Sequential comparison queue |
| Simultaneous Copies | 1 | Uses global progress tracker |
| Report Generation | Unlimited | Each user gets unique filename |

### Report Naming Convention
```
ascf_<mode>_report_<timestamp>.html
Example: ascf_archives_report_20251210_030204.html
```
- Each comparison generates unique timestamp
- No conflict between users

### Best Practices for Multi-User
1. Use different browsers/sessions for testing
2. Monitor server console for errors
3. Avoid simultaneous large comparisons (performance)
4. Reports are saved to server - share via URL

---

# Slide 16: Setup Instructions

## 📋 Setup Instructions

### Prerequisites
| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.8+ | Required |
| Java JDK | 8+ | For decompilation |
| CFR Decompiler | 0.152+ | Optional but recommended |
| Network Access | - | For network share paths |

### Step 1: Install Python
```powershell
# Windows: Download from python.org or use winget
winget install Python.Python.3.12
```

### Step 2: Install CFR Decompiler (Optional)
```powershell
# Download CFR from GitHub releases
# https://github.com/leibnitz27/cfr/releases
# Save to C:\cfr-0.152.jar

# Set environment variable (permanent)
[Environment]::SetEnvironmentVariable("CFR_JAR", "C:\cfr-0.152.jar", "Machine")

# Or set per-session
$env:CFR_JAR = "C:\cfr-0.152.jar"
```

### Step 3: Start Server
```powershell
cd C:\PythonProject_UPDATED\python_project
$env:CFR_JAR = "C:\cfr-0.152.jar"
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

### Step 4: Access Tool
```
Open browser: http://<server-ip>:5000
Example: http://192.168.1.41:5000
```

### Step 5: Run Comparison
1. Select comparison mode
2. Enter Old Build Path
3. Enter New Build Path
4. Click "Run Comparison"
5. View HTML report

---

# Quick Reference Card

## 🚀 Quick Commands

### Start Server (Fresh)
```powershell
$env:CFR_JAR="C:\cfr-0.152.jar"; cd C:\PythonProject_UPDATED\python_project; python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

### Restart Server (Clear Cache)
```powershell
taskkill /F /PID $(netstat -ano | findstr ":5000 " | ForEach-Object { $_.Split()[-1] }) 2>$null; Remove-Item -Recurse -Force __pycache__,src\__pycache__ -ErrorAction SilentlyContinue; Start-Sleep 2; $env:CFR_JAR="C:\cfr-0.152.jar"; python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

### Find Server Process
```powershell
netstat -ano | findstr ":5000" | findstr "LISTENING"
```

### Access URLs
```
Local:   http://localhost:5000
Network: http://<your-ip>:5000
```

---

# 📞 Support

## Contact Information

For issues or feature requests:
1. Check server console for error messages
2. Review this documentation
3. Contact the development team

---

*ASCF Patch Comparison Tool - Version 1.0*
*Last Updated: December 2024*




