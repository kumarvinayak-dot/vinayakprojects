# CRM Build Verification Runbook — JAR Manifest & Class Diff

Plain-text checklist: [CRM_BUILD_VERIFICATION_ONE_LINE.txt](CRM_BUILD_VERIFICATION_ONE_LINE.txt)

**Purpose:** Verify a new CRM SDK build using **JAR names for manifest diff** and **JAR names for class diff** on the comparison tool.

**Example old build (_67):** `\\dvciisilon02\cm_share2\v10.24.1\v10.24.1_67\SDK\Output\SDK_CRM_10.24.1.0_linux64_20260508_1421.jar`  
**Example new build (_68):** `\\dvciisilon02\cm_share2\v10.24.1\v10.24.1_68\SDK\Output\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar`  
**Tool URL:** http://localhost:5000/crm (localhost only)

Open the illustrated runbook in a browser (screenshots are **embedded** — safe to open from any folder):

**[CRM_BUILD_VERIFICATION_RUNBOOK.html](CRM_BUILD_VERIFICATION_RUNBOOK.html)**

Regenerate after screenshot changes:

```bat
python scripts\build_crm_build_verification_runbook_html.py
```

Source PNGs are in **docs/crm_build_verification_screenshots/** (11 images). The HTML builder embeds them as base64.

## One-line steps

1. Start the comparison tool (Desktop shortcut or start_server.bat) and open http://localhost:5000/crm.
2. Check Build Comparison on the home page.
3. Select CRM/AMSS only (do not select ASCF, UXF/OMNI, or DEX).
4. Enter the previous CRM SDK path in Old build and the new CRM SDK path in New build.
5. In JAR names for manifest diff (comma-separated), enter JAR names or patterns (e.g. AmdocsSecurityManager.jar, scala*.jar) to verify MANIFEST.MF Implementation-Version between old and new builds.
6. In JAR names for class diff (comma-separated), enter JAR names (e.g. ClfyBillingManager.jar, ClfyCore.jar) to compare decompiled Java/class changes in the report.
7. Leave Full content comparison as the default CRM mode.
8. Click Run comparison and wait for the HTML report to open.
9. In the report, review the JAR manifest diff table for old vs new Implementation-Version per JAR and path.
10. In the report, review the JAR class diff section for Perforce-style Java source differences for the JARs you listed.
11. Confirm manifest versions and class changes match expected build verification results before sign-off.

## Regenerate screenshots

```powershell
powershell -Sta -ExecutionPolicy Bypass -File scripts\capture_crm_build_verification_runbook.ps1
python scripts\build_crm_build_verification_runbook_html.py
```

## Related runbooks

- Full CRM automation (paths, console, environment validation): [CRM_AUTOMATION_RUNBOOK.md](CRM_AUTOMATION_RUNBOOK.md)
