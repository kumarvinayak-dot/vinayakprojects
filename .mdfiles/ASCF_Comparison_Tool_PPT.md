---
marp: true
theme: default
paginate: true
backgroundColor: #fff
style: |
  section {
    font-family: 'Segoe UI', Arial, sans-serif;
  }
  h1 {
    color: #2563eb;
  }
  h2 {
    color: #1e40af;
  }
  code {
    background: #f1f5f9;
    padding: 2px 6px;
    border-radius: 4px;
  }
  pre {
    background: #1e293b;
    color: #e2e8f0;
    padding: 16px;
    border-radius: 8px;
  }
  table {
    font-size: 0.85em;
  }
  .columns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }
---

<!-- _class: lead -->
<!-- _backgroundColor: #1e40af -->
<!-- _color: white -->

# 🔍 ASCF Patch Comparison Tool

## Complete User Guide & Technical Documentation

**Version 1.0 | December 2024**

---

# 📋 Agenda

1. Tool Overview
2. Key Features
3. Comparison Modes
   - Full Content | JAR Naming | Archives | FOSS
4. RPM CPIO Comparison
5. Copy Mode
6. Technical Architecture
7. Server Management
8. Debugging Guide
9. Multi-User Support
10. Setup Instructions

---

# 🎯 Tool Overview

## What is ASCF Comparison Tool?

A **web-based tool** for comparing software builds, patches, and archives.

### Key Capabilities
- 📁 Compare files between two directories
- 📊 Identify size changes and content differences
- 🔄 Detect version changes in JAR files
- ☕ View decompiled Java class differences
- 📦 Deep scan nested archives (EAR → WAR → JAR)

### Access URL
```
http://<server-ip>:5000
Example: http://192.168.1.41:5000
```

---

# ✨ Key Features

| Feature | Description |
|---------|-------------|
| 🔍 **Multi-Mode Comparison** | 4 different comparison modes |
| 📦 **Nested Archive Support** | EAR → WAR → JAR → Class files |
| 📦 **RPM CPIO Support** | Linux package comparison |
| ☕ **Java Decompilation** | CFR decompiler integration |
| 📋 **Copy with Progress** | Real-time progress bar |
| 📊 **HTML Reports** | Interactive reports with navigation |
| 👥 **Multi-User Support** | Concurrent users |
| 🔄 **Progress Tracking** | Real-time ETA |

---

# 🔄 Comparison Modes Overview

```
┌─────────────────────────────────────────────────────────┐
│                  COMPARISON MODES                        │
├─────────────────┬───────────────────────────────────────┤
│ Full Content    │ Complete file-by-file comparison      │
├─────────────────┼───────────────────────────────────────┤
│ JAR Naming      │ JAR version/naming analysis           │
├─────────────────┼───────────────────────────────────────┤
│ Archives        │ Deep EAR/WAR/JAR/ZIP/RPM scan         │
├─────────────────┼───────────────────────────────────────┤
│ FOSS Foundation │ Specific JAR MANIFEST analysis        │
└─────────────────┴───────────────────────────────────────┘
```

---

# 📁 Mode 1: Full Content Comparison

## What It Does
- Compares **every file** in two directories
- Identifies: Added, Removed, Modified, Unchanged
- Shows **size differences** with percentages
- **Color-coded** results (Red = grew, Green = shrunk)

## Logic Flow
```
1. Walk through both directories
2. Build file inventory (path, size, hash)
3. Compare inventories:
   ├── Only in Old → REMOVED
   ├── Only in New → ADDED
   ├── Different hash → MODIFIED
   └── Same hash → UNCHANGED
```

---

# 📦 Mode 2: JAR Naming Convention

## What It Does
- Compares JAR file **names** between builds
- Identifies **version pattern changes**
- Detects **renamed JARs**

## Example Analysis
```
commons-logging-1.2.jar → commons-logging-1.3.jar
        ↓
   VERSION CHANGE DETECTED

log4j-core-2.14.1.jar → log4j-core-2.17.1.jar
        ↓
   SECURITY UPDATE DETECTED
```

---

# 📦 Mode 3: Archive Contents

## Supported Archive Types

| Type | Extension | Contains |
|------|-----------|----------|
| Enterprise Archive | `.ear` | WAR, JAR |
| Web Archive | `.war` | Classes, JARs |
| Java Archive | `.jar` | Classes |
| ZIP Archive | `.zip` | Any files |
| RPM Package | `.rpm` | CPIO payload |

## Nested Comparison
```
EAR File
  └── WAR File
       └── WEB-INF/lib/
            └── JAR Files
                 └── .class Files
                      └── Decompiled Java Source
```

---

# 📦 Mode 4: RPM CPIO Comparison

## RPM File Structure

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
│ │   └── ./opt/config/*.xml        │ │
│ └─────────────────────────────────┘ │
└─────────────────────────────────────┘
```

---

# 📦 RPM Parsing Logic

## How It Works

```python
1. Find gzip signature (0x1f 0x8b) in RPM
2. Decompress CPIO data
3. Parse CPIO newc format:
   - Magic: 070701 or 070702
   - Extract header fields
   - Get file content
4. For nested archives:
   - Recursively scan EAR/WAR/JAR
5. Compare all extracted members
```

## Report Shows
- 📊 RPM Summary Stats
- ⚠️ Content Changes Table
- ✅ New Files | ❌ Removed Files
- 📁 Structure Tree View

---

# 🔍 FOSS Foundation Mode

## What It Does
- Compares specific JAR file versions
- Analyzes **MANIFEST.MF** files
- Useful for **FOSS compliance** checks

## MANIFEST.MF Analysis
```
Manifest-Version: 1.0
Bundle-Name: Commons Logging
Bundle-SymbolicName: org.apache.commons.logging
Bundle-Version: 1.2.0
Implementation-Version: 1.2
```

Compares: `Implementation-Version`, `Bundle-Version`, `Specification-Version`

---

# 📋 Copy Mode

## Features
- ✅ Copy files/folders with **real-time progress**
- 📊 Shows bytes copied, files processed
- ⏱️ **Estimated time remaining**
- ⚠️ Graceful error handling
- 📝 Reports skipped files

## Progress UI
```
┌─────────────────────────────────────────────┐
│ 📋 Copy in Progress                         │
│ ████████████████░░░░░░░░░░░░ 52%           │
│                                             │
│ Files: 156 / 300                            │
│ Size: 45.2 MB / 87.1 MB                    │
│ Current: WEB-INF/lib/commons-io.jar        │
│ ETA: 2 minutes 15 seconds                   │
└─────────────────────────────────────────────┘
```

---

# 🏗️ Technical Architecture

```
┌──────────────────────────────────────────────┐
│              Web Browser                      │
│           (HTML5 + JavaScript)               │
└─────────────────────┬────────────────────────┘
                      │ HTTP
┌─────────────────────▼────────────────────────┐
│         ThreadingHTTPServer                   │
│  ┌─────────┬──────────┬───────────────────┐  │
│  │ do_GET  │ do_POST  │ /progress polling │  │
│  └─────────┴──────────┴───────────────────┘  │
└─────────────────────┬────────────────────────┘
                      │
┌─────────────────────▼────────────────────────┐
│           Comparison Engine                   │
│  FolderComparer | ArchiveContentsComparer    │
│  NamingConventionComparer | JarManifest      │
└──────────────────────────────────────────────┘
```

---

# 🛠️ Tools & Technologies

## Core Stack

| Component | Technology |
|-----------|------------|
| Backend | Python 3.8+ |
| Web Server | ThreadingHTTPServer |
| Frontend | HTML5 + CSS3 + JavaScript |
| Archive Processing | zipfile (built-in) |
| RPM Processing | gzip + CPIO parser |

## Java Decompilation

| Tool | Purpose |
|------|---------|
| **CFR** | Decompile .class to .java |
| **javap** | Fallback disassembler |

Set: `$env:CFR_JAR = "C:\cfr-0.152.jar"`

---

# 🖥️ Server Management

## Start Server
```powershell
cd C:\PythonProject_UPDATED\python_project
$env:CFR_JAR = "C:\cfr-0.152.jar"
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

## Stop Server
```powershell
# Find PID
netstat -ano | findstr ":5000" | findstr "LISTENING"

# Kill process
taskkill /F /PID <process_id>
```

## Or use Ctrl+C in terminal

---

# 🗑️ Cache Management

## Why Clear Cache?
- Code changes not reflecting
- Stale comparison results
- Import errors after updates

## Clear Cache Commands
```powershell
# Clear Python cache
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force src\__pycache__ -ErrorAction SilentlyContinue
```

## Full Restart (One-Liner)
```powershell
taskkill /F /PID <pid>; Remove-Item -Recurse -Force __pycache__,src\__pycache__ -ErrorAction SilentlyContinue; Start-Sleep 2; python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

---

# 🔧 Debugging Guide

## Common Issues

| Error | Solution |
|-------|----------|
| **Access Denied** | Run as Admin / Auth to share |
| **No results** | Verify paths exist |
| **Progress stuck** | Restart server + clear cache |
| **Decompile fails** | Check CFR_JAR env variable |

## Debug Tips
```powershell
# Check server process
netstat -ano | findstr ":5000"

# Check CFR setup
echo $env:CFR_JAR
java -jar C:\cfr-0.152.jar --help
```

---

# 👥 Multi-User Support

## Architecture
```
     ThreadingHTTPServer
            │
    ┌───────┼───────┬───────┐
    │       │       │       │
  User1   User2   User3   UserN
 Thread1 Thread2 Thread3 ThreadN
```

## Limits

| Resource | Limit |
|----------|-------|
| Concurrent Connections | ~100 |
| Simultaneous Comparisons | 1 (queued) |
| Report Generation | Unlimited |

Reports use unique timestamps - no conflicts!

---

# 📋 Setup Instructions

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Python | 3.8+ |
| Java JDK | 8+ (optional) |
| CFR Decompiler | 0.152+ (optional) |

## Quick Setup
```powershell
# 1. Navigate to project
cd C:\PythonProject_UPDATED\python_project

# 2. Set CFR (optional)
$env:CFR_JAR = "C:\cfr-0.152.jar"

# 3. Start server
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo

# 4. Open browser
http://192.168.1.41:5000
```

---

# 🚀 Quick Reference

## Start Server
```powershell
$env:CFR_JAR="C:\cfr-0.152.jar"
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

## Access URLs
- Local: `http://localhost:5000`
- Network: `http://192.168.1.41:5000`

## Example Paths
```
\\INPNQASCFLB02\uif_10250\uif\ASCF_GA_10250pb00\DU
\\INPNQASCFLB02\uif_10250\uif\ASCF_HF_10250pb00hf01\DU
```

---

<!-- _class: lead -->
<!-- _backgroundColor: #1e40af -->
<!-- _color: white -->

# 🎉 Thank You!

## ASCF Patch Comparison Tool

**Questions?**

---

<!-- _backgroundColor: #f1f5f9 -->

# 📚 Appendix: File Locations

| File | Path |
|------|------|
| Server Script | `C:\PythonProject_UPDATED\python_project\builtin_server.py` |
| Reports | `C:\PythonProject_UPDATED\python_project\ascf_*_report_*.html` |
| CFR JAR | `C:\cfr-0.152.jar` |

## Report Naming
```
ascf_<mode>_report_<YYYYMMDD_HHMMSS>.html
```

Example: `ascf_archives_report_20251210_030204.html`




