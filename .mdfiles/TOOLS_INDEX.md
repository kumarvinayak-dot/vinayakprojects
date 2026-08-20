# Folder Comparison Tool - Tools Index

## 🚀 Launch Scripts (Double-Click to Start)

| File | Description | Requirements |
|------|-------------|--------------|
| `start_server.bat` | Launch web interface on http://127.0.0.1:5000 | Flask |
| `start_gui.bat` | Launch standalone desktop GUI | Python only |

## 🐍 Python Applications

| File | Description | How to Run |
|------|-------------|------------|
| `web_app.py` | Flask web server | `py web_app.py` |
| `compare_folders_gui.py` | Tkinter desktop GUI | `py compare_folders_gui.py` |
| `run_comparison_standalone.py` | Command-line tool | `py run_comparison_standalone.py` |

## 📚 Documentation Files

| File | Purpose |
|------|---------|
| `README.md` | Complete project overview and guide |
| `LAUNCH_INSTRUCTIONS.md` | Detailed launch and usage instructions |
| `QUICK_START.md` | Quick reference card |
| `INSTALL_FLASK_OFFLINE.md` | Offline Flask installation guide |
| `FIX_SUMMARY.md` | Recent bug fixes and changes |
| `TOOLS_INDEX.md` | This file - index of all tools |

## 📦 Core Modules

| File | Description |
|------|-------------|
| `src/folder_compare.py` | Core comparison logic (3500+ lines) |
| `requirements.txt` | Python package dependencies |

## 🗂️ Output Folders

| Folder | Contents |
|--------|----------|
| `reports/` | Generated comparison reports (TXT and HTML) |
| `templates/` | Web interface HTML templates |

## 📝 Configuration Files

| File | Purpose |
|------|---------|
| `requirements.txt` | List of required Python packages |

---

## Quick Decision Tree

```
Do you need to compare folders?
│
├─ YES → Which interface do you prefer?
│        │
│        ├─ Browser/Web → Is Flask installed?
│        │                │
│        │                ├─ YES → Run: start_server.bat
│        │                │         Then open: http://127.0.0.1:5000
│        │                │
│        │                └─ NO → Install Flask (see INSTALL_FLASK_OFFLINE.md)
│        │                         OR use Desktop GUI instead ↓
│        │
│        ├─ Desktop GUI → Run: start_gui.bat
│        │
│        └─ Command Line → Run: py run_comparison_standalone.py
│
└─ NO → Need help?
         │
         ├─ Installation → See: INSTALL_FLASK_OFFLINE.md
         ├─ Quick Start → See: QUICK_START.md
         ├─ Full Guide → See: LAUNCH_INSTRUCTIONS.md
         └─ Overview → See: README.md
```

---

## Test Files

| File | Purpose |
|------|---------|
| `test_naming_fix.py` | Test naming convention comparison |

Run tests with: `py test_naming_fix.py`

---

## Recommended Starting Points

### For First-Time Users
1. Read `QUICK_START.md`
2. Run `start_gui.bat` (easiest option, no Flask needed)
3. Try a small test comparison

### For Web Interface Users
1. Check if Flask is installed: `py -m flask --version`
2. If not installed, see `INSTALL_FLASK_OFFLINE.md`
3. Run `start_server.bat`
4. Open http://127.0.0.1:5000

### For Advanced Users
1. Read `README.md` for complete overview
2. Read `LAUNCH_INSTRUCTIONS.md` for all options
3. Use any interface (web, GUI, or CLI)

---

## File Sizes (Approximate)

- `src/folder_compare.py`: ~120 KB (3500 lines)
- `web_app.py`: ~15 KB
- `compare_folders_gui.py`: ~10 KB
- Documentation files: ~50 KB total

---

## Last Updated
November 27, 2025











