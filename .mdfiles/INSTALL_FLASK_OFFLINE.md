# Installing Flask Offline

## Prerequisites
- Python installed on both machines
- Network access on one machine to download packages
- Method to transfer files (USB, network share, etc.)

## Step 1: Download Flask Wheels (Machine WITH Internet)

Open Command Prompt or PowerShell and run:

```bash
# Create a directory for downloads
mkdir C:\flask_wheels
cd C:\flask_wheels

# Download Flask and all dependencies
pip download flask

# OR if you want a specific version:
pip download flask==3.0.0

# This will download approximately 7-10 .whl files including:
# - flask-3.0.x-py3-none-any.whl
# - Werkzeug-3.0.x-py3-none-any.whl
# - Jinja2-3.1.x-py3-none-any.whl
# - itsdangerous-2.x.x-py3-none-any.whl
# - click-8.1.x-py3-none-any.whl
# - blinker-1.x.x-py3-none-any.whl
# - MarkupSafe-2.1.x-cp3xx-cp3xx-win_amd64.whl
```

## Step 2: Transfer Files

Copy the entire `C:\flask_wheels` folder to your target machine:

**Option A: Network Share**
```
Copy from: C:\flask_wheels
Copy to: \\YOURMACHINE\C$\Users\SVINAYAK\python_project\flask_wheels
```

**Option B: USB Drive**
```
Copy C:\flask_wheels to USB drive
Then copy to: C:\Users\SVINAYAK\python_project\flask_wheels
```

## Step 3: Install Flask (Machine WITHOUT Internet)

### Method A: Using the Batch Script (Easiest)

1. Ensure all .whl files are in: `C:\Users\SVINAYAK\python_project\flask_wheels\`
2. Double-click: `install_flask_offline.bat`
3. Follow the prompts

### Method B: Manual Installation

Open PowerShell in the project directory:

```powershell
cd C:\Users\SVINAYAK\python_project

# Install from local wheels
py -m pip install --no-index --find-links flask_wheels flask
```

## Step 4: Verify Installation

```powershell
py -c "import flask; print('Flask version:', flask.__version__)"
```

You should see: `Flask version: 3.0.x`

## Step 5: Start Web Server

```powershell
# Double-click start_server.bat
# OR run:
py web_app.py
```

Then open your browser to: http://127.0.0.1:5000

---

## Alternative: Use the Simple GUI (No Flask Needed!)

If you can't install Flask, use the tkinter GUI instead:

```powershell
# Double-click start_gui.bat
# OR run:
py compare_folders_gui.py
```

This works immediately with no dependencies!

---

## Troubleshooting

### "No matching distribution found"
- Make sure ALL wheel files are in the `flask_wheels` directory
- Flask needs about 7-10 dependency files

### "Module not found" after installation
- Restart your terminal/PowerShell
- Try: `py -m pip list | findstr flask`

### Still having issues?
Use the tkinter GUI - it's simpler and requires no installation!











