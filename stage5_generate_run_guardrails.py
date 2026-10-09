# Stage 5 - Dynamic Script Generation -> Guardrails -> REAL Execution
# Run:  py -3.12 stage5_generate_run_guardrails.py
#
# Full Point-4 pipeline:
#   1. GENERATE an executable Playwright test from a requirement (LLM)
#   2. GUARDRAIL 1 - Safety scan (static, blocks destructive ops)
#   3. GUARDRAIL 2 - Requirement match (LLM-as-judge, >=7/10)
#   4. GUARDRAIL 3 - Human approval gate
#   5. EXECUTE the generated test for real with Playwright, report PASS/FAIL
#
# Nothing executes until all guardrails pass. Then it actually runs.
#
# One-time setup:
#   pip install openai playwright
#   python -m playwright install chromium

import os
import re
import subprocess
import sys
from datetime import datetime
from openai import OpenAI

client = OpenAI(base_url="https://api.groq.com/openai/v1",
                api_key=os.environ.get("GROQ_API_KEY"))
MODEL = "openai/gpt-oss-120b"

def ask_llm(system, user):
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}])
    return r.choices[0].message.content


# ---------- 1. GENERATE ----------
def generate_script(requirement: str, url: str, visible: bool = False) -> str:
    launch_line = (
        "browser = p.chromium.launch(headless=False, slow_mo=1200)" if visible
        else "browser = p.chromium.launch(headless=True)"
    )
    system = (
        "You are a test automation engineer. Generate a single self-contained Python "
        "Playwright script (sync API) for the requirement. Requirements:\n"
        "- Use: from playwright.sync_api import sync_playwright, expect\n"
        f"- Launch the browser EXACTLY like this: {launch_line}\n"
        "- Create page, perform the steps, assert with expect()\n"
        "- Use the EXACT start URL provided\n"
        "- After the final assertion, if visible mode, add: page.wait_for_timeout(2500)\n"
        "- Print 'TEST PASSED' at the end if assertions hold\n"
        "- NO destructive actions (no os, subprocess, file writes, shell)\n"
        "- Return ONLY runnable code, no markdown fences, no explanation."
    )
    mode = "VISIBLE (headed, slow) so a human can watch the flow" if visible else "headless"
    user = f"Requirement: {requirement}\nStart URL: {url}\nMode: {mode}\n\nGenerate the full runnable Playwright script."
    code = ask_llm(system, user)
    code = re.sub(r"^```[a-zA-Z]*\n?", "", code.strip())
    code = re.sub(r"\n?```$", "", code.strip())
    return code.strip()


# ---------- GUARDRAIL 1: safety ----------
FORBIDDEN = [r"\bos\.system\b", r"\bsubprocess\b", r"\bshutil\b", r"\bos\.remove\b",
             r"\bos\.unlink\b", r"\beval\s*\(", r"\bexec\s*\(", r"\b__import__\b",
             r"\bopen\s*\([^)]*['\"]w", r"\bDROP\s+TABLE\b", r"\bDELETE\s+FROM\b"]

def guardrail_safety(code):
    hits = [p for p in FORBIDDEN if re.search(p, code, re.IGNORECASE)]
    return (len(hits) == 0), hits


# ---------- GUARDRAIL 2: requirement match ----------
def guardrail_match(requirement, code):
    system = ("Score 0-10 how well this Playwright script tests the requirement. "
              "Respond EXACTLY:\nSCORE: <n>\nREASON: <one line>")
    result = ask_llm(system, f"REQUIREMENT:\n{requirement}\n\nSCRIPT:\n{code}")
    score, reason = 0, ""
    for line in result.splitlines():
        if line.upper().startswith("SCORE:"):
            d = "".join(c for c in line if c.isdigit()); score = int(d) if d else 0
        if line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return score, reason


# ---------- GUARDRAIL 3: human approval ----------
def guardrail_human(interactive):
    if not interactive:
        print("\n[GUARDRAIL 3] Human approval SKIPPED (demo mode).")
        return True
    ans = input("\n[GUARDRAIL 3] Type 'approve' to run this test: ").strip().lower()
    return ans == "approve"


# ---------- helper: save the generated script to a folder ----------
GENERATED_DIR = "generated_tests"

def save_script(requirement: str, code: str) -> str:
    """Save the generated script to generated_tests/ with a readable name. Returns the path."""
    os.makedirs(GENERATED_DIR, exist_ok=True)
    # build a safe short slug from the requirement
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", requirement.lower())[:40].strip("_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"test_{slug}_{timestamp}.py"
    path = os.path.join(GENERATED_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return path

# ---------- 5. EXECUTE for real (runs the SAVED script) ----------
def execute_script(path: str):
    """Run the saved script for real and report PASS/FAIL. The file is kept, not deleted."""
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=90)
        out = proc.stdout + proc.stderr
        passed = "TEST PASSED" in proc.stdout and proc.returncode == 0
        return passed, out.strip()[-800:]
    except subprocess.TimeoutExpired:
        return False, "Execution timed out."


# ---------- Orchestrate ----------
def run_pipeline(requirement, url, interactive=False, visible=False):
    print("=" * 60)
    print(f"REQUIREMENT: {requirement}\nURL: {url}")
    print("=" * 60)

    print("\n[1] Generating Playwright script...")
    code = generate_script(requirement, url, visible=visible)
    print("-" * 60); print(code); print("-" * 60)

    # Save the generated script to the generated_tests/ folder (kept for the record)
    saved_path = save_script(requirement, code)
    print(f"\n[SAVED] Generated script saved to: {saved_path}")

    print("\n[GUARDRAIL 1] Safety scan...")
    safe, hits = guardrail_safety(code)
    if not safe:
        print(f"  BLOCKED - forbidden: {hits}"); return
    print("  PASSED.")

    print("\n[GUARDRAIL 2] Requirement match...")
    score, reason = guardrail_match(requirement, code)
    print(f"  Score {score}/10 | {reason}")
    if score < 7:
        print("  BLOCKED - low match."); return
    print("  PASSED.")

    if not guardrail_human(interactive):
        print("  REJECTED by human."); return
    print("  APPROVED.")

    print("\n[5] EXECUTING the saved test for real...")
    passed, output = execute_script(saved_path)
    print("-" * 60); print(output); print("-" * 60)
    print(f"\n[RESULT] Test execution: {'PASSED' if passed else 'FAILED'}")


if __name__ == "__main__":
    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY not set.")
    else:
        # Public automation-practice site (built for exactly this)
        # SET visible=True to WATCH the browser perform the flow live (headed + slow).
        # SET visible=False for fast headless runs (CI / cloud).
        run_pipeline(
            requirement="Log in with username 'tomsmith' and password 'SuperSecretPassword!' and verify the secure-area success message appears.",
            url="https://the-internet.herokuapp.com/login",
            interactive=True,
            visible=True,   # <-- watch the flow. Change to False for headless.
        )
