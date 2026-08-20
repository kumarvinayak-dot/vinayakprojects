# CRM Automation Runbook — Build Verification

Plain-text checklist: [../CRM_AUTOMATION_RUN_STEPS.txt](../CRM_AUTOMATION_RUN_STEPS.txt)

**Old build (_67):** `\\dvciisilon02\cm_share2\v10.24.1\v10.24.1_67\SDK\Output\SDK_CRM_10.24.1.0_linux64_20260508_1421.jar`  
**New build (_68):** `\\dvciisilon02\cm_share2\v10.24.1\v10.24.1_68\SDK\Output\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar`  
**Tool URL:** http://localhost:5000/crm (localhost only)

Open the illustrated runbook in a browser (screenshots are **embedded** — safe to open from any folder):

**[CRM_AUTOMATION_RUNBOOK.html](CRM_AUTOMATION_RUNBOOK.html)**

Regenerate after screenshot or content changes:

```bat
powershell -Sta -ExecutionPolicy Bypass -File scripts\capture_crm_runbook.ps1
python scripts\build_crm_automation_runbook_html.py
```

Source PNGs are in **docs/crm_runbook_screenshots/** (16 images). The HTML builder embeds them as base64.

## New features covered

- **Install:** `Comparison Tool.zip` → `INSTALL.cmd` → `launch_comparison_tool.bat`
- **CRM options:** Fast full compare, ClfyAgent nested compare, manifest/class diff, Find JAR (SSH), crm-playbooks Nexus TAR
- **Report:** Version-changed JARs (blue), Missing JARs, ClfyAgent BC table, 10% size alerts, release-aligned path counts
- **Environment validation** + Find JAR for class on deployment host

See also: [CRM_BUILD_VERIFICATION_RUNBOOK.html](CRM_BUILD_VERIFICATION_RUNBOOK.html) for manifest & class diff detail.

## One-line steps

1. Open the received new CRM build UNC path in Windows Explorer and confirm you have read access.
2. Note the previous bundle CRM build UNC path to use as the Old build baseline for comparison.
3. Double-click `launch_comparison_tool.bat` (or `start_server.bat`) to start the tool on localhost.
4. Open http://localhost:5000/crm in Chrome or Edge when the browser does not open automatically.
5. Check the Build Comparison checkbox on the home page.
6. Check the CRM/AMSS application checkbox only (do not select ASCF, UXF/OMNI, or DEX).
7. Paste the v10.24.1_67 SDK JAR path into the Old build field.
8. Paste the v10.24.1_68 SDK JAR path into the New build field.
9. Optionally click Load CRM base paths and use Use as Old / Use as New to pick paths from the version table.
10. Configure Fast CRM/AMSS compare, optional ClfyAgent, manifest/class diff, or Nexus fields.
11. Click Run comparison and allow the progress tab to open (enable pop-ups if blocked).
12. Watch Console Output until the comparison finishes and the HTML report opens automatically.
13. Review Version-changed JARs, Missing JARs, ClfyAgent table, size alerts, and aligned path mismatch sections.
14. Save or share the HTML report from `\\YOUR-PC\c$\crm_compare_reports` if sign-off is required.
15. Optionally enable Environment Validation and Find JAR for class (SSH) on the deployment server.
16. Run `STOP_SERVER.bat` when verification is complete.
