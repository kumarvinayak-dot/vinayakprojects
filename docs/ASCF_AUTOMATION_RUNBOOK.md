# ASCF Automation Runbook — HF09 vs HF10

Plain-text checklist (same steps): [../ASCF_AUTOMATION_RUN_STEPS.txt](../ASCF_AUTOMATION_RUN_STEPS.txt)

**Old build:** `\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf09`  
**New build:** `\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf10`  
**Tool URL:** http://localhost:5000/crm

Open the illustrated runbook in a browser (screenshots are **embedded** — safe to open from any folder):

**[docs/ASCF_AUTOMATION_RUNBOOK.html](ASCF_AUTOMATION_RUNBOOK.html)**

Regenerate after screenshot updates:

```bat
python scripts\build_ascf_automation_runbook_html.py
```

Source PNGs are in **docs/runbook_screenshots/** (22 images). The HTML builder embeds them as base64.

## ASCF comparison modes (home page)

Select **ASCF** only, then choose **one** mode:

| Mode | Purpose |
|------|---------|
| **Full content comparison** | Default HF build verification — full tree diff |
| **Naming convention comparison** | Archive/JAR filename pairing across builds |
| **Archives contents** | Deep compare inside .ear/.war/.zip/.rpm/.jar |
| **FOSS/Foundation** | Single JAR `Implementation-Version` check |

When **Full content** is selected, optional report fields appear:

- JAR names for **class diff** (CFR decompilation)
- File patterns for **text/config diffs**
- JAR names for **manifest diff**
- **Explicit file sizes** to compare

FOSS/Foundation mode also offers Foundation product / Foss product dropdowns and network retry settings.

See the **Feature reference** section at the bottom of the HTML runbook for full details.

## One-line steps

1. Open the received new build UNC path in Windows Explorer and confirm you have read access.
2. Note the previous HF build UNC path to use as the Old build baseline for comparison.
3. Double-click `start_server.bat` in the python_project folder to start the automation tool server.
4. Open http://localhost:5000/crm in Chrome or Edge (use your network URL if running on a shared server).
5. Check the Build Comparison checkbox on the home page.
6. Check the ASCF application checkbox (select only ASCF — not CRM, UXF, or DEX).
7. Paste `\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf09` into the Old build field.
8. Paste `\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf10` into the New build field.
9. Optionally click Load ASCF base paths and use Use as Old / Use as New to populate paths from the table.
10. Select one ASCF comparison mode (Full content, Naming convention, Archives contents, or FOSS/Foundation). For Full content, configure optional class/manifest/config/size fields if needed.
11. Click Run comparison and allow the progress tab to open (enable pop-ups if the browser blocks it).
12. Watch Console Output until the comparison finishes and the HTML report opens automatically.
13. Review the report — use Expand all / Collapse all; check Added/Removed and optional manifest/class/config sections.
14. Save or share the HTML report from `\\YOUR-PC\c$\crm_compare_reports` if sign-off is required.
15. Optionally enable Environment Validation, enter SSH host/user/password and remote path, and validate JAR Implementation-Version on the deployment server.
16. After build verification passes, click Foundations Link on the home page.
17. In Step 1 (Prepare), select ASCF Product Version `10.24.0.pb01`, enter HF number `10`, and click Prepare Foundation Link.
18. In Step 2 (Update), enter ASCF version, HF `10`, month `Jun`, year `2026`, and ASCFDesignerUpdateSite creation date `20260609`, then click Update Foundation Link.
19. Click Update Index.html, enter ASCF version, HF number, month, and year, then submit.
20. Click Update Javadoc.html, enter ASCF Product Version and HF number `10`, then submit.
21. Return to the home page, click Administrative Tool, fill New Hotfix Version `10.24.0.pb01_hf10`, Release Date, Download URL, and Release Notes URL, then click Submit & Launch Automation.
22. Verify the pre-filled Foundation admin form in the opened browser window and click Add Hotfix to publish the release.
