"""
ai_testgen.py — Agentic AI Test-Case Generation module
Self-contained: RAG + LangGraph + DeepEval. Imported by web_app.py.

Place this file in the same folder as web_app.py.
Also place crm_requirements.txt in the same folder.
Requires GROQ_API_KEY environment variable.
"""

import os
from openai import OpenAI
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
from langgraph.graph import StateGraph, END
from typing import TypedDict

# ---- LLM (Groq) ----
_client = OpenAI(base_url="https://api.groq.com/openai/v1",
                 api_key=os.environ.get("GROQ_API_KEY"),
                 max_retries=0)  # _chat handles rate-limit retries itself
_MODEL = "openai/gpt-oss-120b"
_JUDGE_MODEL = "openai/gpt-oss-20b"  # DeepEval judge (Stage 6); separate Groq daily quota


class LLMQuotaError(RuntimeError):
    """Groq refused the call: daily quota used up, or still rate-limited after retries."""

# ---- Load & index requirements once at import (lazy) ----
_embedder = None
_index = None
_chunks = None

def _init():
    global _embedder, _index, _chunks
    if _index is not None:
        return
    req_path = os.path.join(os.path.dirname(__file__), "crm_requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        _chunks = [c.strip() for c in f.read().split("\n\n") if c.strip()]
    _embedder = SentenceTransformer("all-MiniLM-L6-v2")
    vecs = _embedder.encode(_chunks).astype("float32")
    _index = faiss.IndexFlatL2(len(vecs[0]))
    _index.add(vecs)

def _retrieve(query, k=2):
    _init()
    qv = _embedder.encode([query]).astype("float32")
    _, idx = _index.search(qv, k)
    return [_chunks[i] for i in idx[0]]

def _quota_message(err):
    """Readable message for a Groq 429, or None when it is a short per-minute limit."""
    import re as _re
    text = str(err)
    wait = _re.search(r"try again in ([0-9hms.]+)", text)
    wait = f" Try again in about {wait.group(1).split('.')[0]}s." if wait else ""
    if "per day" in text or "(TPD)" in text or "(RPD)" in text:
        model = _re.search(r"model `([^`]+)`", text)  # Groq names the model that hit its limit
        model = model.group(1) if model else _MODEL
        return (f"Groq daily limit reached for the free tier on {model}.{wait} "
                "Upgrade at console.groq.com/settings/billing for more.")
    return None


def _chat(messages, retries=6, model=_MODEL, **options):
    """Chat completion that handles Groq rate limits (HTTP 429).
    A daily limit fails fast with a clear message; a per-minute limit waits for
    Groq's retry-after (capped at 30 s) and retries."""
    import time
    from openai import APIStatusError, RateLimitError
    for attempt in range(retries):
        try:
            return _client.chat.completions.create(model=model, messages=messages, **options)
        except RateLimitError as e:  # 429 - must come before APIStatusError (its parent)
            daily = _quota_message(e)
            if daily:
                raise LLMQuotaError(daily) from e
            if attempt == retries - 1:
                raise LLMQuotaError("Groq per-minute rate limit (8,000 tokens/minute) is still "
                                    "exceeded after retries. Wait a minute and try again.") from e
            try:
                wait = float(e.response.headers.get("retry-after", ""))
            except (AttributeError, ValueError):
                wait = 2 ** (attempt + 1)
            time.sleep(min(max(wait, 1), 30))
        except APIStatusError as e:  # 413 - one request bigger than the per-minute budget
            if e.status_code != 413:
                raise
            raise LLMQuotaError("This request is larger than the Groq free tier allows in one "
                                "call (8,000 tokens/minute). Shorten the App UI details or "
                                "upgrade the Groq plan.") from e


def _ask(system, user, **options):
    r = _chat([{"role": "system", "content": system},
               {"role": "user", "content": user}], **options)
    return r.choices[0].message.content

# ---- LangGraph state ----
class _State(TypedDict):
    query: str
    context: list
    test_cases: str
    score: int
    feedback: str
    attempts: int

def _generate(state):
    state["attempts"] += 1
    state["context"] = _retrieve(state["query"])
    system = ("You are a QA test-case generator. Using ONLY the provided context, "
              "generate 3 test cases with Title, Steps, Expected Result, including an "
              "edge case. Format clearly with headings.")
    user = f"CONTEXT:\n{chr(10).join(state['context'])}\n\nGenerate test cases for: {state['query']}"
    if state.get("feedback"):
        user += f"\n\nImprove based on this feedback: {state['feedback']}"
    state["test_cases"] = _ask(system, user)
    return state

def _validate(state):
    system = ("You are a QA reviewer. Score the test cases 0-10 for grounding in the "
              "context and edge coverage. Respond EXACTLY as:\nSCORE: <n>\nFEEDBACK: <one line>")
    user = f"CONTEXT:\n{chr(10).join(state['context'])}\n\nTEST CASES:\n{state['test_cases']}"
    result = _ask(system, user)
    score, feedback = 0, ""
    for line in result.splitlines():
        if line.upper().startswith("SCORE:"):
            d = "".join(c for c in line if c.isdigit())
            score = int(d) if d else 0
        if line.upper().startswith("FEEDBACK:"):
            feedback = line.split(":", 1)[1].strip()
    state["score"], state["feedback"] = score, feedback
    return state

def _decide(state):
    if state["score"] >= 8 or state["attempts"] >= 3:
        return "done"
    return "refine"

def _build_graph():
    wf = StateGraph(_State)
    wf.add_node("generate", _generate)
    wf.add_node("validate", _validate)
    wf.set_entry_point("generate")
    wf.add_edge("generate", "validate")
    wf.add_conditional_edges("validate", _decide, {"refine": "generate", "done": END})
    return wf.compile()

_app_graph = None

def generate_test_cases(query: str) -> dict:
    """Main entry point called by the Flask route.
    Returns a dict with context, test_cases, score, feedback, attempts."""
    global _app_graph
    if not os.environ.get("GROQ_API_KEY"):
        return {"error": "GROQ_API_KEY not set on the server."}
    if _app_graph is None:
        _app_graph = _build_graph()
    initial = {"query": query, "context": [], "test_cases": "",
               "score": 0, "feedback": "", "attempts": 0}
    try:
        final = _app_graph.invoke(initial)
    except LLMQuotaError as e:
        return {"error": str(e)}
    return {
        "query": query,
        "context": "\n\n".join(final["context"]),
        "test_cases": final["test_cases"],
        "score": final["score"],
        "feedback": final["feedback"],
        "attempts": final["attempts"],
    }


# =====================================================================
# Playwright script generation -> Guardrails -> Stage 6 human approval -> Execute
# Reuses the Stage 5 guardrails (safety scan + requirement match).
# Nothing executes unless guardrails passed AND a human approved.
# =====================================================================
import hashlib
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime

import stage5_generate_run_guardrails as _stage5
from stage5_generate_run_guardrails import guardrail_safety, guardrail_match

# Stage 5's judge has its own Groq client with no rate-limit handling; route it through _ask
_stage5.ask_llm = lambda system, user: _ask(system, user)

_GENERATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_tests")
_MATCH_THRESHOLD = 7
_EXEC_TIMEOUT = 120

# script_id -> {query, requirement, code, sha, path, guardrails, approval, execution}
_scripts = {}
_scripts_lock = threading.Lock()


def _sha(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _script_rules(visible):
    """System prompt shared by script generation and script repair."""
    launch_line = ("browser = p.chromium.launch(headless=False, slow_mo=800)" if visible
                   else "browser = p.chromium.launch(headless=True)")
    return (
        "You are a test automation engineer. Convert the given test cases into ONE "
        "self-contained Python Playwright script (sync API). Requirements:\n"
        "- Use: from playwright.sync_api import sync_playwright, expect\n"
        f"- Launch the browser EXACTLY like this: {launch_line}\n"
        "- Use the EXACT start URL provided\n"
        "- Implement each test case as its own function, assert with expect()\n"
        "- If APP UI DETAILS are provided, use ONLY the selectors, URLs, test users and data "
        "listed there - never invent selectors - and automate every test-case step they make possible\n"
        "- Assert each value only on the element the UI details document for that value, using the "
        "exact message wording they give; when a test case's example data (IDs, product codes) is "
        "not available in the app, use the closest documented value instead\n"
        "- Every assertion must be final and correct - no placeholder or 'to be refined' assertions\n"
        "- Use ONLY real Playwright Python assertions: to_have_text, to_contain_text, to_have_count, "
        "to_be_visible, to_be_hidden, to_be_enabled, to_be_disabled, to_have_value, to_have_url, "
        "to_have_attribute (and their not_to_ forms). There is NO to_have_count_at_least or similar; "
        "for 'at least N' use expect(locator.nth(N - 1)).to_be_visible()\n"
        "- to_have_text / to_contain_text take a string or re.compile(...) - never a lambda or "
        "function\n"
        "- Keep every string literal, including f-strings, on a single line\n"
        "- Never guess message wording: if the UI details do not give a message, assert on a "
        "documented element (e.g. the status) instead\n"
        "- Playwright locators are STRICT: an assertion on a locator that matches more than one "
        "element fails. When several elements can match (table rows, audit entries, line items), "
        "first narrow to ONE row by its unique text, then assert inside it, e.g. "
        "expect(page.locator('#audit-log .audit-entry', has_text='Order cancelled')"
        ".locator('.audit-user')).to_have_text('csr1'). Never filter only by a value that can "
        "repeat (such as a user ID); use .first / .nth(i) or to_have_count when needed\n"
        "- Only assert a total count (to_have_count on a list or table) when the UI details state "
        "that exact count for that exact scenario; otherwise assert that the specific expected "
        "row exists. Seeded/pre-existing records may have different history than records the "
        "test creates itself\n"
        "- Each test function must navigate to the page it needs itself (do not rely on the page "
        "left by a previous test), and use unique new IDs so tests do not collide\n"
        "- Skip steps that cannot be automated against the given URL and add a comment why\n"
        "- Print 'TEST PASSED' at the very end only if all assertions hold\n"
        "- NO destructive actions (no os, subprocess, shutil, file writes, shell, eval, exec)\n"
        "- Return ONLY runnable code, no markdown fences, no explanation."
    )


def _llm_code(system, user):
    """Ask the LLM for a script and strip markdown fences; retries once on empty output."""
    for _ in range(2):  # the model occasionally returns no content; ask once more
        code = _ask(system, user, temperature=0) or ""
        code = re.sub(r"^```[a-zA-Z]*\n?", "", code.strip())
        code = re.sub(r"\n?```$", "", code.strip()).strip()
        if code:
            return code
    raise RuntimeError("The LLM returned an empty Playwright script. Please try again.")


def _build_script(query, test_cases, url, visible, ui_details=""):
    """LLM-generate the Playwright script for the test cases and save it. Returns (code, path)."""
    user = f"Requirement area: {query}\nStart URL: {url}\n\nTEST CASES:\n{test_cases}\n\n"
    if ui_details:
        user += f"APP UI DETAILS (pages, selectors, test data):\n{ui_details}\n\n"
    user += "Generate the full runnable Playwright script."
    code = _llm_code(_script_rules(visible), user)
    return code, _save_script(query, code)


def _save_script(query, code):
    os.makedirs(_GENERATED_DIR, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", query.lower())[:40].strip("_") or "script"
    path = os.path.join(_GENERATED_DIR,
                        f"test_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return path


def _requirement(query, url, test_cases):
    return f"Requirement area: {query}\nStart URL: {url}\n\nTest cases:\n{test_cases}"


def generate_playwright_script(query, test_cases, url, visible=False, ui_details=""):
    if not os.environ.get("GROQ_API_KEY"):
        return {"error": "GROQ_API_KEY not set on the server."}
    try:
        code, path = _build_script(query, test_cases, url, visible, ui_details)
    except RuntimeError as e:
        return {"error": str(e)}
    script_id = uuid.uuid4().hex
    with _scripts_lock:
        _scripts[script_id] = {"query": query, "test_cases": test_cases, "url": url,
                               "visible": visible, "ui_details": ui_details,
                               "requirement": _requirement(query, url, test_cases),
                               "code": code, "sha": _sha(code), "path": path,
                               "guardrails": None, "approval": None, "execution": None,
                               "case_scores": None}
    return {"script_id": script_id, "code": code, "path": path}


def fetch_ui_map(url):
    """Fetch <origin>/ui-map from the target app (the mock CRM serves one)."""
    from urllib.parse import urlsplit
    from urllib.request import urlopen
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return {"error": "Enter a valid target URL first."}
    map_url = f"{parts.scheme}://{parts.netloc}/ui-map"
    try:
        with urlopen(map_url, timeout=5) as r:
            return {"ui_details": r.read(20000).decode("utf-8", "replace"), "source": map_url}
    except Exception as e:
        return {"error": f"Could not load {map_url}: {e}. Paste your app's UI details instead."}


def check_playwright_api(code):
    """Guardrail 1b - static API check: the script must parse, and every
    expect(...).<assertion>() must exist in the installed Playwright.
    Catches invented calls such as to_have_count_at_least() before approval."""
    import ast
    from playwright.sync_api import APIResponseAssertions, LocatorAssertions, PageAssertions
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Python syntax error on line {e.lineno}: {e.msg}"]
    valid = {n for cls in (LocatorAssertions, PageAssertions, APIResponseAssertions)
             for n in dir(cls) if n.startswith(("to_", "not_to_"))}
    unknown, bad_args = set(), set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name) and node.value.func.id == "expect"
                and node.attr not in valid):
            unknown.add(node.attr)
        # expect(...).to_have_text(lambda ...) - assertions take a value, not a function
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "expect"
                and any(isinstance(a, ast.Lambda) for a in node.args)):
            bad_args.add(node.func.attr)
    return ([f"expect(...).{name}() does not exist in Playwright" for name in sorted(unknown)]
            + [f"expect(...).{name}() was given a lambda; it needs a string or re.compile(...)"
               for name in sorted(bad_args)])


_MAX_REPAIRS = 3


def _compact_traceback(output, limit=1500):
    """Keep the script's own frames and the error; drop Playwright/library internals."""
    kept, skip = [], False
    for line in output.splitlines():
        if line.startswith('  File "'):
            skip = "site-packages" in line
        elif not line.startswith(" "):
            skip = False
        if not skip and line.strip():
            kept.append(line)
    return "\n".join(kept)[-limit:]


def repair_script(script_id):
    """Self-healing step: send the failing script and its exact error back to the LLM
    for a targeted fix. The fix is a NEW version of the script, so the guardrails and
    the human approval (Stage 6) are required again before it can run."""
    rec = _scripts.get(script_id)
    if not rec:
        return {"error": "Unknown script. Generate the Playwright script again."}
    if rec.get("repairs", 0) >= _MAX_REPAIRS:
        return {"error": f"This script was already fixed {_MAX_REPAIRS} times. Regenerate it "
                         "or improve the App UI details instead."}
    guard, run = rec["guardrails"] or {}, rec["execution"] or {}
    if guard.get("api_errors"):
        failure = ("The static Playwright API check blocked the script:\n"
                   + "\n".join(guard["api_errors"]))
    elif run and not run.get("passed"):
        failure = "Running the script failed with this output:\n" + _compact_traceback(run["output"])
    else:
        return {"error": "Nothing to fix: the script has not failed the API check or execution."}
    # the test cases are left out: the script already encodes them, and the free tier
    # caps a single request at 8,000 tokens
    user = f"Requirement area: {rec['query']}\nStart URL: {rec['url']}\n\n"
    if rec["ui_details"]:
        user += f"APP UI DETAILS (pages, selectors, test data):\n{rec['ui_details']}\n\n"
    user += (f"CURRENT SCRIPT:\n{rec['code']}\n\nFAILURE:\n{failure}\n\n"
             "Fix the script so this failure cannot happen. Change only what is needed and keep "
             "the parts that work. Where an assertion contradicts the APP UI DETAILS, follow the "
             "UI details. Return the full corrected script.")
    try:
        code = _llm_code(_script_rules(rec["visible"]), user)
    except RuntimeError as e:
        return {"error": str(e)}
    path = _save_script(rec["query"], code)
    with _scripts_lock:
        rec.update({"code": code, "sha": _sha(code), "path": path, "guardrails": None,
                    "approval": None, "execution": None, "repairs": rec.get("repairs", 0) + 1})
    return {"script_id": script_id, "code": code, "path": path, "repairs": rec["repairs"],
            "max_repairs": _MAX_REPAIRS, "fixed_failure": failure[:600]}


def run_guardrails(script_id):
    rec = _scripts.get(script_id)
    if not rec:
        return {"error": "Unknown script. Generate the Playwright script again."}
    safe, hits = guardrail_safety(rec["code"])
    api_errors = check_playwright_api(rec["code"]) if safe else []
    score, reason = (0, "Skipped - safety scan failed.")
    if api_errors:
        score, reason = (0, "Skipped - script uses Playwright APIs that do not exist.")
    if safe and not api_errors:
        try:  # guardrail_match (Stage 5) now calls _ask, which retries per-minute limits
            score, reason = guardrail_match(rec["requirement"], rec["code"])
        except LLMQuotaError as e:
            return {"error": str(e)}
    result = {"safe": safe, "hits": hits, "api_errors": api_errors,
              "match_score": score, "match_reason": reason, "threshold": _MATCH_THRESHOLD,
              "passed": safe and not api_errors and score >= _MATCH_THRESHOLD}
    with _scripts_lock:
        rec["guardrails"] = result
        rec["approval"] = None  # any earlier approval no longer applies
    return result


def record_approval(script_id, decision, reviewer, note):
    """Stage 6 - human-in-the-loop validation."""
    rec = _scripts.get(script_id)
    if not rec:
        return {"error": "Unknown script. Generate the Playwright script again."}
    if not (rec["guardrails"] and rec["guardrails"]["passed"]):
        return {"error": "Guardrails have not passed - human approval is not allowed yet."}
    if not reviewer:
        return {"error": "Reviewer name is required."}
    approval = {"decision": "approved" if decision == "approve" else "rejected",
                "reviewer": reviewer, "note": note,
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with _scripts_lock:
        rec["approval"] = approval
    return approval


def execute_script(script_id):
    rec = _scripts.get(script_id)
    if not rec:
        return {"error": "Unknown script. Generate the Playwright script again."}
    if not (rec["guardrails"] and rec["guardrails"]["passed"]):
        return {"error": "Blocked: guardrails have not passed."}
    if not (rec["approval"] and rec["approval"]["decision"] == "approved"):
        return {"error": "Blocked: script has not been approved by a human (Stage 6)."}
    with open(rec["path"], "r", encoding="utf-8") as f:
        if _sha(f.read()) != rec["sha"]:
            return {"error": "Blocked: script file changed on disk after it was validated."}
    try:
        proc = subprocess.run([sys.executable, rec["path"]], capture_output=True, text=True,
                              timeout=_EXEC_TIMEOUT, cwd=_GENERATED_DIR)
        output = (proc.stdout + proc.stderr).strip()
        passed = "TEST PASSED" in proc.stdout and proc.returncode == 0
    except subprocess.TimeoutExpired:
        output, passed = f"Execution timed out after {_EXEC_TIMEOUT}s.", False
    result = {"passed": passed, "output": output[-4000:], "path": rec["path"]}
    with _scripts_lock:
        rec["execution"] = result
    return result


# ---- Stage 6: per-case DeepEval scoring shown to the human reviewer ----
_CASE_THRESHOLD = 0.7
_CASE_MAX_ATTEMPTS = 2
_CASE_HEADER = re.compile(r"(?im)^[\s#*_>]*test[\s_-]*case\s*\d+\b")


def split_test_cases(text):
    """Split generator output into one string per test case (keeps each heading)."""
    starts = [m.start() for m in _CASE_HEADER.finditer(text)]
    if not starts:
        return [text.strip()] if text.strip() else []
    bounds = starts + [len(text)]
    return [text[bounds[i]:bounds[i + 1]].strip().rstrip("-").strip()
            for i in range(len(starts))]


def _case_title(case_text):
    first = case_text.strip().splitlines()[0] if case_text.strip() else ""
    first = first.strip("#*_> ").strip()
    return re.sub(r"(?i)^title\s*:\s*\**\s*", "", first)


def _groq_judge():
    from deepeval.models.base_model import DeepEvalBaseLLM

    class GroqJudge(DeepEvalBaseLLM):
        def __init__(self):
            pass

        def load_model(self):
            return _client

        def generate(self, prompt: str) -> str:
            r = _chat([{"role": "user", "content": prompt}], model=_JUDGE_MODEL)
            return r.choices[0].message.content

        async def a_generate(self, prompt: str) -> str:
            return self.generate(prompt)

        def get_model_name(self):
            return f"Groq {_JUDGE_MODEL}"

    return GroqJudge()


def _regenerate_case(query, context, original_case, reason):
    """Stage 6: regenerate only a test case that failed scoring."""
    system = (
        "You are a QA test-case generator. The following test case scored poorly "
        f"because: {reason}\n\n"
        "Using ONLY the provided context, generate an improved version.\n"
        "Format EXACTLY:\n"
        "Title: <title>\n"
        "Steps: <steps>\n"
        "Expected Result: <expected result>"
    )
    user = (f"CONTEXT:\n{chr(10).join(context)}\n\n"
            f"ORIGINAL (weak) TEST CASE:\n{original_case}\n\n"
            f"Generate an improved test case for query: {query}")
    return _ask(system, user).strip()


def _score_case(query, context, case_text, judge):
    from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric
    from deepeval.test_case import LLMTestCase

    tc = LLMTestCase(input=query, actual_output=case_text, retrieval_context=context)
    faith = FaithfulnessMetric(model=judge, threshold=_CASE_THRESHOLD,
                               include_reason=True, async_mode=False)
    faith.measure(tc)
    rel = AnswerRelevancyMetric(model=judge, threshold=_CASE_THRESHOLD,
                                include_reason=True, async_mode=False)
    rel.measure(tc)
    return {"faithfulness": round(faith.score, 2), "faith_reason": faith.reason,
            "relevancy": round(rel.score, 2), "rel_reason": rel.reason,
            "passed": faith.score >= _CASE_THRESHOLD and rel.score >= _CASE_THRESHOLD}


def score_test_cases(script_id):
    """Stage 6 - per-case DeepEval scoring + selective refinement.
    Each case is scored individually; only failing cases are regenerated and rescored.
    If any case was refined, the Playwright script is rebuilt from the refined cases and
    the guardrails are rerun, so the reviewer approves the script that will actually run.
    Cached per script once every case scored without an error."""
    import time
    rec = _scripts.get(script_id)
    if not rec:
        return {"error": "Unknown script. Generate the Playwright script again."}
    if rec.get("case_scores"):
        return rec["case_scores"]

    context = _retrieve(rec["query"])
    judge = _groq_judge()
    cases, quota_error = [], None
    for i, original in enumerate(split_test_cases(rec["test_cases"]), 1):
        current, history, entry = original, [], {}
        if quota_error:  # daily quota gone: don't spend minutes failing the remaining cases
            cases.append({"error": quota_error, "passed": False, "case_number": i,
                          "title": _case_title(current), "attempts": 0, "history": [],
                          "refined": False, "final_case": current})
            continue
        if i > 1:
            time.sleep(3)  # pace the judge calls to stay under the per-minute rate limit
        for attempt in range(1, _CASE_MAX_ATTEMPTS + 1):
            try:
                if attempt > 1:
                    current = _regenerate_case(rec["query"], context, current, last_reason)
                entry = _score_case(rec["query"], context, current, judge)
            except Exception as e:
                entry = {"error": str(e)[:300], "passed": False}
                if "daily limit" in str(e):
                    quota_error = str(e)
                break
            history.append({"attempt": attempt, "faithfulness": entry["faithfulness"],
                            "relevancy": entry["relevancy"], "passed": entry["passed"]})
            if entry["passed"]:
                break
            last_reason = (f"Faithfulness={entry['faithfulness']} ({entry['faith_reason']}); "
                           f"Relevancy={entry['relevancy']} ({entry['rel_reason']})")
        entry.update({"case_number": i, "title": _case_title(current),
                      "attempts": len(history), "history": history,
                      "refined": current != original, "final_case": current})
        cases.append(entry)

    result = {"threshold": _CASE_THRESHOLD, "max_attempts": _CASE_MAX_ATTEMPTS,
              "cases": cases, "passed_count": sum(1 for c in cases if c["passed"]),
              "refined_count": sum(1 for c in cases if c["refined"]),
              "error_count": sum(1 for c in cases if c.get("error"))}

    if result["refined_count"]:
        refined_text = "\n\n---\n\n".join(
            c["final_case"] if not c["refined"]
            else f"**Test Case {c['case_number']} (refined)**\n\n{c['final_case']}"
            for c in cases)
        try:
            code, path = _build_script(rec["query"], refined_text, rec["url"], rec["visible"],
                                       rec["ui_details"])
        except RuntimeError as e:
            # the existing script no longer matches the refined cases - block approval
            with _scripts_lock:
                rec["guardrails"] = {"safe": False, "hits": [], "match_score": 0,
                                     "match_reason": str(e), "threshold": _MATCH_THRESHOLD,
                                     "passed": False}
                rec["approval"] = None
            return {"error": f"{e} Test cases were refined, so regenerate the Playwright script."}
        with _scripts_lock:
            rec.update({"test_cases": refined_text, "code": code, "sha": _sha(code),
                        "path": path,
                        "requirement": _requirement(rec["query"], rec["url"], refined_text)})
        result["test_cases"] = refined_text
        result["script"] = {"code": code, "path": path}
        result["guardrails"] = run_guardrails(script_id)

    if not result["error_count"]:  # errored scores are not cached, so Retry rescores
        with _scripts_lock:
            rec["case_scores"] = result
    return result
