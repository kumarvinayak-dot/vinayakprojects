# ASCF FOSS & Foundations Adoption Runbook

Plain-text checklist: [ASCF_FOSS_FOUNDATIONS_ADOPTION_ONE_LINE.txt](ASCF_FOSS_FOUNDATIONS_ADOPTION_ONE_LINE.txt)

**Purpose:** Verify Foundation and FOSS JAR adoption between ASCF builds, then publish the HF to the Foundation portal.

**Example old build (HF09):** `\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf09`  
**Example new build (HF10):** `\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf10`  
**ASCF product version (example):** `10.24.0.pb01` | **HF:** `10`  
**Tool URL:** http://localhost:5000/crm (localhost only)

Open the illustrated runbook in a browser:

**[docs/ASCF_FOSS_FOUNDATIONS_ADOPTION_RUNBOOK.html](ASCF_FOSS_FOUNDATIONS_ADOPTION_RUNBOOK.html)**

Screenshots are in **docs/ascf_foss_foundations_screenshots/** (22 images, one per step).

## Part 1 — FOSS & Foundation JAR verification

1. Start the comparison tool (Desktop shortcut or start_server.bat) and open http://localhost:5000/crm.
2. Check Build Comparison on the home page.
3. Select ASCF only (do not select CRM/AMSS, UXF/OMNI, or DEX).
4. Enter the previous ASCF HF build path in Old build and the new ASCF HF build path in New build.
5. In ASCF comparison mode, select FOSS/Foundation (JAR Implementation-Version).
6. For a Foundation JAR, choose Foundation product (e.g. Amdocs Core, Amdocs Security Manager) — the Jar name field fills automatically — or type the JAR name manually.
7. For a FOSS library, choose Foss product (e.g. log4j-core.jar, commons-io.jar) — the Jar name field fills automatically — or type the JAR name manually.
8. Click Run comparison and wait for the FOSS/Foundation HTML report to open.
9. In the report, confirm Implementation-Version (old build) vs Implementation-Version (new build) and JAR paths match the expected adoption.
10. Repeat steps 6–9 for each Foundation and FOSS JAR on your adoption checklist until all required libraries are verified.
11. Optionally enable Environment Validation on the home page, enter SSH host/user/password and the remote ASCF deployment path, and validate Foundation/FOSS JAR Implementation-Version on the target environment.

## Part 2 — Foundations site adoption

12. With ASCF selected on the home page, click Foundations Link at the bottom of the page.
13. In Step 1 (Prepare ASCF Foundation Link), select ASCF Product Version, enter the new HF number (2 digits), and click Prepare Foundation Link.
14. Confirm the prepare result shows the new download.html was created from the previous HF template.
15. In Step 2 (Update Foundation Link), select the same ASCF Product Version, enter the current HF number, Month, Year, and ASCFDesignerUpdateSite creation date (YYYYMMDD), then click Update Foundation Link.
16. Click Update Index.html, enter ASCF version, HF number, month, and year, then submit and confirm the index file was updated.
17. Click Update Javadoc.html, enter ASCF Product Version and HF number, then submit and confirm the javadoc page was updated.
18. Optionally click Update API manually and follow the on-screen links if API documentation must be updated for this HF.
19. Return to the home page and click Administrative Tool (enabled when ASCF is selected).
20. Enter New Hotfix Version (e.g. 10.24.0.pb01_hf10), Release Date, Download URL, and Release Notes URL, then click Submit & Launch Automation.
21. In the opened Foundation admin browser window, verify the pre-filled hotfix form matches the new HF release details.
22. Click Add Hotfix to publish the release on the Foundation site and confirm the new HF appears in the ASCF hotfix list.

## Regenerate screenshots

```powershell
powershell -Sta -ExecutionPolicy Bypass -File scripts\capture_ascf_foss_foundations_runbook.ps1
```

## Related runbooks

- Full ASCF automation (build compare + Foundations): [ASCF_AUTOMATION_RUNBOOK.md](ASCF_AUTOMATION_RUNBOOK.md)
