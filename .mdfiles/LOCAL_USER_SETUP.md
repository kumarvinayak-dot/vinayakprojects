# Comparison Tool — Local User Setup

**Developed by Vinayak Srivastava**

Quick reference for users who install the comparison tool on their own Windows PC.

---

## Install (ZIP package — recommended)

1. Extract **`Comparison Tool.zip`** anywhere on your PC (e.g. `C:\Comparison Tool\`).
2. Open the extracted **`Comparison Tool`** folder and double-click **`setup.cmd`**.
3. Setup installs Python packages into **`.venv`** in that same folder (no file copy).
4. Run **`start_server.bat`** in that folder.
5. Open **http://localhost:5000/crm** in your browser.

See **`INSTALL_INSTRUCTIONS.html`** or **`INSTALL_INSTRUCTIONS.md`** in the ZIP for full details.

---

## Daily use

| Action | Command |
|--------|---------|
| Start (recommended) | `launch_comparison_tool.bat` |
| Start (server only) | `start_server.bat` |
| Open UI | **http://localhost:5000/crm** |
| Stop | `STOP_SERVER.bat` |
| Check status | `CHECK_STATUS.bat` |
| Uninstall | `Uninstall Comparison Tool.bat` |

---

## Run comparisons (all applications)

Single URL: **http://localhost:5000/crm** (aliases: `/ascf`, `/uxf`, `/dex`)

| Application | Select on UI | Typical inputs |
|-------------|--------------|----------------|
| **ASCF** | ASCF | Old/new ASCF build paths; **Load ASCF base paths** |
| **CRM / AMSS** | CRM/AMSS | Old/new CRM or AMSS paths; **Load CRM** or **AMSS base paths** |
| **UXF / OMNI** | UXF/OMNI | Old/new UXF or OMNI paths; **Load UXF** or **OMNI base paths** |
| **DEX** | DEX | Nexus previous/latest versions (digital-playbook, digital-webapp-ces) |

1. Enable **Build Comparison**.
2. Select **one** application checkbox.
3. Enter paths or Nexus versions; configure options for that app.
4. **Run comparison** → open HTML report when done.

**Environment Validation** (same page): SSH to a Linux host for deployment checks.

Reports: `\\<your-pc>\c$\crm_compare_reports`

---

## Requirements

| Requirement | Details |
|-------------|---------|
| **OS** | Windows 10 or 11 (64-bit) |
| **Python** | 3.10 or newer — [python.org](https://www.python.org/downloads/), check **Add Python to PATH** |
| **Disk space** | ~50 GB free on **C:** for large comparisons |
| **Network** | Read access to corporate NAS/build paths |
| **Browser** | Chrome or Edge |

---

## Notes

- Server runs on **localhost only** by default — no network URLs are exposed.
- No Desktop or Start Menu shortcuts are created during ZIP install.
- Distribution is **ZIP-only** (no `.exe` installer) to avoid antivirus false positives.
- Optional HTTPS: run `scripts\generate_https_cert.ps1`, then use `https://localhost:5443/crm`.
- Optional CRM Java diffs: place `cfr-0.152.jar` at `C:\cfr-0.152.jar`.

---

## Troubleshooting

| Problem | What to do |
|---------|------------|
| **Python not found** | Install Python with Add to PATH, then run `setup.cmd` again |
| **Port 5000 in use** | Run `STOP_SERVER.bat`, then `start_server.bat` |
| **Browser can't connect** | Use `http://localhost:5000/crm` only (not a network IP) |
| **pip install fails** | Ask your team lead for offline wheels |

---

## Advanced: shared Windows Server (LAN)

For IT-managed server deployment only (not typical laptop users):

- `INSTALL_PERSISTENT_SERVER.bat` — scheduled task + `.env` token
- `start_server_lan.bat` — bind on all interfaces with `CRM_API_TOKEN`

Local users should use **`start_server.bat`** only.
