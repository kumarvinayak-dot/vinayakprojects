# Comparison Tool — Installation Instructions

**Developed by Vinayak Srivastava**

Each user installs and runs the tool **on their own PC**. The server listens on **localhost only** (`127.0.0.1`). Other computers cannot connect. No shared network HTTP/HTTPS URLs are used.

---

## What you receive

Your team lead shares one file:

| File | Purpose |
|------|---------|
| **`Comparison Tool.zip`** | ZIP installer package for your PC |

The ZIP includes this document (`INSTALL_INSTRUCTIONS.md`), a browser-friendly **`INSTALL_INSTRUCTIONS.html`**, **`INSTALL.cmd`** (recommended installer), and **`launch_comparison_tool.bat`** (daily launcher). There is **no `.exe` installer** — this avoids antivirus false positives on corporate PCs.

---

## Before you install

| Requirement | Details |
|-------------|---------|
| **PC** | Windows 10 or 11 (64-bit) |
| **Python** | **3.10 or newer** must be installed ([python.org](https://www.python.org/downloads/)) — check **“Add Python to PATH”** during install |
| **Disk space** | ~50 GB free on **C:** for large build comparisons |
| **Network** | Access to corporate NAS/build paths (e.g. `\\ntnet\...`, `\\INPNQASCFLB02\...`) |
| **Browser** | Chrome or Edge |
| **Optional** | `C:\cfr-0.152.jar` for Java source diff in CRM reports |

---

## Install (one time)

1. Copy **`Comparison Tool.zip`** to your PC (e.g. Desktop, Downloads, or `C:\`).
2. **Right-click** the ZIP → **Extract All…** (or unzip with 7-Zip / WinRAR).
3. Open the extracted **`Comparison Tool`** folder (paths with spaces are fine, e.g. `C:\Comparison Tool\Comparison Tool`).
4. **Double-click** **`INSTALL.cmd`** in that folder (recommended). (`setup.cmd` still works for compatibility.)
5. Wait for setup to finish. It only:
   - creates a **`.venv`** folder in the same directory, and
   - installs Python packages from **`requirements.txt`** using a **clean pip install** (no cache — avoids confusing pip warnings).
   No files are copied to another location. This may take **5–15 minutes** depending on network speed.
6. When you see **Installation complete**, press Enter.

No Desktop or Start Menu shortcuts are created. Use the batch files in the extracted folder.

---

## Use the tool (every day)

1. Open the install folder and double-click **`launch_comparison_tool.bat`** (or **`start_server.bat`**).
2. Wait a few seconds — your browser should open **http://localhost:5000/crm** automatically.
3. If it does not, open manually:
   ```
   http://localhost:5000/crm
   ```
   The same page is also available at `/ascf`, `/uxf`, and `/dex` (aliases).
4. Run your build comparison or environment validation (see next section).
5. To stop the server, run **`STOP_SERVER.bat`** in the install folder.

---

## Run comparisons (all applications)

One web UI supports **ASCF**, **CRM/AMSS**, **UXF/OMNI**, and **DEX**. Use a **single URL**: **http://localhost:5000/crm**

### Quick steps (every comparison)

| Step | Action |
|------|--------|
| 1 | Start the server (`launch_comparison_tool.bat`). |
| 2 | Open **http://localhost:5000/crm** in Chrome or Edge. |
| 3 | Enable **Build Comparison**. |
| 4 | Under **Application**, select **exactly one**: ASCF, CRM/AMSS, UXF/OMNI, or DEX. |
| 5 | Enter **Old build path** and **New build path** (UNC NAS paths), or use the version **Load … base paths** buttons. |
| 6 | Choose application-specific options (see table below). |
| 7 | Click **Run comparison**. Watch **Console Output** for live progress. |
| 8 | When finished, open the HTML report from the results panel. |

### Application-specific notes

| Application | What to select | Paths / inputs | Common options |
|-------------|----------------|----------------|----------------|
| **ASCF** | `ASCF` | Old vs new ASCF build roots (e.g. `\\INPNQASCFLB02\...`). Use **Load ASCF base paths**. | Full content, naming convention, archives (`.ear`/`.war`/`.zip`/`.jar`), or FOSS/Foundation (JAR manifest). |
| **CRM / AMSS** | `CRM/AMSS` | Old vs new CRM or AMSS build folders. Use **Load CRM base paths** or **Load AMSS base paths**. | **Fast CRM/AMSS full compare** (recommended). Optional **Compare ClfyAgent** for nested SDK archives. CRM Nexus verification: fill previous/latest Nexus versions when using playbook TAR mode. |
| **UXF / OMNI** | `UXF/OMNI` | Old vs new UXF or OMNI build roots. Use **Load UXF base paths** or **Load OMNI base paths**. | Full folder comparison. |
| **DEX** | `DEX` | Enter previous/latest Nexus versions for **digital-playbook** and/or **digital-webapp-ces** (no NAS paths required). | Downloads artifacts from Nexus, compares, and generates an HTML report. |

### Environment validation (all applications)

On the same home page, use **Environment Validation** to SSH to a Linux host and verify deployment paths, JARs, and configuration — independent of build comparison.

### Reports and temp files

| Location | Purpose |
|----------|---------|
| `\\<your-pc>\c$\crm_compare_reports` | Saved HTML reports |
| `\\<your-pc>\c$\crm_compare_temp` | Temporary files during large comparisons |

### Optional: Java source diff (CRM)

Place **`cfr-0.152.jar`** at **`C:\cfr-0.152.jar`** for decompiled `.class` diffs in CRM reports.

---

## Install folder location

The install folder is **wherever you extracted the ZIP**, for example:

```
C:\Comparison Tool\Comparison Tool
```
or
```
C:\Users\<YourUsername>\Desktop\Comparison Tool
```

Important files:

| File | Purpose |
|------|---------|
| `launch_comparison_tool.bat` | **Recommended** — start server and open browser (HTTP) |
| `INSTALL.cmd` | One-time install (creates `.venv`, installs packages) |
| `start_server.bat` | Start server only (localhost HTTP) |
| `STOP_SERVER.bat` | Stop server |
| `CHECK_STATUS.bat` | Check if server is running |
| `INSTALL_MANIFEST.txt` | Install date, paths, and developer credit |
| `Uninstall Comparison Tool.bat` | Remove the tool |

---

## Uninstall

1. Stop the server (`STOP_SERVER.bat`).
2. Run **`Uninstall Comparison Tool.bat`** in the install folder.
3. Type **YES** when prompted.

---

## Troubleshooting

### “Python 3.10+ not found”

Install Python from [python.org](https://www.python.org/downloads/) and enable **Add Python to PATH**, then run **`INSTALL.cmd`** again.

### Browser shows “Can’t reach this page”

1. Run **`start_server.bat`** again.
2. Run `CHECK_STATUS.bat` in the install folder.
3. Confirm you use **`http://localhost:5000/crm`** (not an IP address or another PC’s name).

### Port 5000 already in use

1. Run `STOP_SERVER.bat`.
2. Start again with **`start_server.bat`**.

### pip install fails (no internet / proxy)

Ask your team lead for offline wheel packages and manual setup (see `LOCAL_USER_SETUP.md` in the project).

### Confusing pip messages during install

`INSTALL.cmd` uses `pip install --no-cache-dir` so you should not see “Cache entry deserialization failed” warnings. If install still fails, delete the `.venv` folder and run **`INSTALL.cmd`** again.

### Windows blocks `INSTALL.cmd`

Right-click **`INSTALL.cmd`** → **Run as administrator**, or open Command Prompt in the extracted folder and run:
```bat
INSTALL.cmd
```

---

## For developers: build the distribution ZIP

From the project root on a Windows dev machine:

```bat
BUILD_INSTALLER.bat
```

Output:

| File | Purpose |
|------|---------|
| `dist\Comparison Tool.zip` | Give this to users |

Distribute **`Comparison Tool.zip`** only. The ZIP contains `INSTALL_INSTRUCTIONS.md`, `INSTALL_INSTRUCTIONS.html`, `INSTALL.cmd`, and `launch_comparison_tool.bat`.

---

## Read-only application files (distribution)

The distribution ZIP ships application source (`.py`, templates, scripts) as **read-only** so users can run comparisons and open `http://localhost:5000/crm` without editing shipped code.

| Writable (runtime) | Read-only (protected) |
|--------------------|------------------------|
| `.venv` (created by `INSTALL.cmd`) | `src\`, `templates\`, `builtin_server.py`, etc. |
| `logs\`, `temp_compare\` | Most other files in the install folder |

`INSTALL.cmd` re-applies the read-only lock after setup. To change code, use a developer checkout — not the end-user ZIP extract.
