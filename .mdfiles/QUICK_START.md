# Folder Comparison Tool - Quick Start Guide

## 🚀 Launch Options

### Option 1: Web Interface (Browser-Based)
```
Double-click: start_server.bat
Then open: http://127.0.0.1:5000
```
**Requires:** Flask installed

### Option 2: Standalone GUI (Desktop App)
```
Double-click: start_gui.bat
```
**Requires:** Python only (no extra packages)

---

## 📋 Quick Steps

1. **Enter Paths:**
   - Folder 1: Your first folder path
   - Folder 2: Your second folder path

2. **Select Mode:**
   - Full Content Comparison (complete file comparison)
   - Naming Convention (filename analysis)
   - Archives Comparison (JAR/WAR/ZIP contents)

3. **Configure:**
   - ☑ Copy network folders locally (for `\\SERVER\path` style paths)

4. **Run:**
   - Click "Start Comparison"
   - Wait for completion
   - View HTML report

---

## 💡 Tips

- **For network paths:** Always enable "Copy network folders locally"
- **Large folders:** Be patient, may take several minutes
- **Reports:** Saved in `reports/` folder
- **Stuck?** Check terminal window for error messages

---

## 🔧 Install Flask (if needed for Web Interface)

**With Internet:**
```powershell
py -m pip install flask
```

**Without Internet:**
See `INSTALL_FLASK_OFFLINE.md` or use Standalone GUI instead

---

## ❌ Stop/Close

- **Web:** Press `Ctrl+C` in terminal window
- **GUI:** Click X button on window

---

## 📖 Full Documentation

See `LAUNCH_INSTRUCTIONS.md` for complete details.











