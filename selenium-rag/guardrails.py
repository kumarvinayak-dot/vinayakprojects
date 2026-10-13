"""
guardrails.py — 3-layer safety guardrails before test execution
Author: Vinayak Kumar Srivastava

Layer 1: Static code analysis (dangerous patterns)
Layer 2: URL whitelist (only run against approved domains)
Layer 3: Human approval gate (mandatory before execution)
"""

import re
from urllib.parse import urlsplit

# This module is deliberately dependency-free (standard library only) so the web app
# (ai_testgen.py) can reuse layers 1 and 2 without the rest of the selenium-rag stack.


# ── Layer 1: Static code analysis ─────────────────────────────────────────────
DANGEROUS_PATTERNS = [
    (r"\bos\.system\b",           "os.system() call — potential shell injection"),
    (r"\bsubprocess\b",           "subprocess usage — potential shell injection"),
    (r"\bopen\s*\(",              "File write/read — not allowed in generated tests"),
    (r"\beval\s*\(",              "eval() — code injection risk"),
    (r"__import__",               "__import__ — dynamic import risk"),
    (r"\bshutil\b",               "shutil usage — filesystem manipulation"),
    (r"\brm\s+-rf\b",             "rm -rf — destructive shell command"),
    (r"driver\.execute_script.*document\.cookie", "Cookie manipulation via JS"),
]

ALLOWED_DOMAINS = [
    "the-internet.herokuapp.com",
    "localhost",
    "127.0.0.1",
]


def layer1_static_analysis(code: str) -> tuple[bool, list[str]]:
    """Scan generated code for dangerous patterns."""
    violations = []
    for pattern, description in DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            violations.append(description)
    return len(violations) == 0, violations


def layer2_url_whitelist(code: str) -> tuple[bool, list[str]]:
    """Ensure every http(s) URL in the script points at a whitelisted host.

    Checks every URL literal, not only driver.get("...") calls, so URLs held in
    constants or f-strings (BASE_URL = "http://...") are covered. The host must
    equal a whitelisted domain or be a subdomain of it: a substring test would
    accept "https://evil.example/?next=localhost".
    """
    violations = []
    for url in sorted(set(re.findall(r"https?://[^\s\"'<>)\\]+", code))):
        host = (urlsplit(url).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            continue
        violations.append(f"URL not in whitelist: {url}")

    return len(violations) == 0, violations


def layer3_human_approval(intent: str, code: str) -> bool:
    """
    Human-in-the-loop approval gate.
    In production: this would send to Slack/email for approval.
    In demo: interactive CLI prompt.
    """
    print("\n" + "="*60)
    print("🔐 HUMAN APPROVAL REQUIRED")
    print("="*60)
    print(f"Intent  : {intent}")
    print(f"Code preview (first 300 chars):\n{code[:300]}...")
    print("="*60)
    answer = input("Approve execution? (yes/no): ").strip().lower()
    return answer in ("yes", "y")


class Guardrails:
    """Run all 3 layers. Raise on any violation."""

    def check(self, intent: str, code: str, skip_human: bool = False) -> bool:
        print("\n[GUARDRAILS] Running 3-layer safety check...")

        # Layer 1
        ok1, v1 = layer1_static_analysis(code)
        if not ok1:
            print(f"[GUARDRAILS] ❌ Layer 1 FAILED: {v1}")
            raise ValueError(f"Static analysis violations: {v1}")
        print("[GUARDRAILS] ✅ Layer 1 (static analysis) passed")

        # Layer 2
        ok2, v2 = layer2_url_whitelist(code)
        if not ok2:
            print(f"[GUARDRAILS] ❌ Layer 2 FAILED: {v2}")
            raise ValueError(f"URL whitelist violations: {v2}")
        print("[GUARDRAILS] ✅ Layer 2 (URL whitelist) passed")

        # Layer 3
        if skip_human:
            print("[GUARDRAILS] ✅ Layer 3 (human approval) — skipped (demo mode)")
            return True

        approved = layer3_human_approval(intent, code)
        if not approved:
            print("[GUARDRAILS] ❌ Layer 3 — Human rejected execution")
            raise PermissionError("Test execution rejected by human reviewer.")
        print("[GUARDRAILS] ✅ Layer 3 (human approval) — approved")
        return True
