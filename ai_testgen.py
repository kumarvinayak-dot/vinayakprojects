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


class _DailyLimitError(LLMQuotaError):
    """This model's daily quota is used up (other models have their own quota)."""


# Backup models, tried in order when a model's DAILY Groq quota is used up. Each model has its
# own daily budget on the free tier. Override with GROQ_FALLBACK_MODELS="model-a,model-b".
_FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]
_exhausted_until = {}   # model -> time.time() until which it is skipped
_models_used = []       # models that answered recent calls (see models_used_since)


def models_used_since(mark):
    """Distinct models that answered calls made after position `mark` (len(_models_used))."""
    return list(dict.fromkeys(_models_used[mark:]))


def _options_for(model, options):
    """Drop request options a model family does not accept."""
    if "gpt-oss" in model:
        return options
    return {k: v for k, v in options.items() if k != "reasoning_effort"}


def _chat(messages, retries=6, model=_MODEL, **options):
    """Chat completion with automatic model fallback.

    Tries `model`; if Groq says that model's daily quota is used up, moves on to the
    backup models. A model that hit its daily limit is skipped for the next 10 minutes."""
    import re as _re
    import time
    chain = [model] + [m for m in _FALLBACK_MODELS if m != model]
    usable = [m for m in chain if time.time() >= _exhausted_until.get(m, 0)] or chain
    last_error = None
    for candidate in usable:
        try:
            response = _chat_one(messages, retries, candidate, **_options_for(candidate, options))
        except _DailyLimitError as e:
            _exhausted_until[candidate] = time.time() + 600
            last_error = e
            print(f"[ai_testgen] daily quota used up for {candidate}; trying the next model",
                  flush=True)
            continue
        message = response.choices[0].message
        if message.content and "<think>" in message.content:  # some models inline their reasoning
            message.content = _re.sub(r"(?s)<think>.*?</think>", "", message.content).strip()
        _models_used.append(candidate)
        return response
    raise LLMQuotaError(f"{last_error} All backup models ({', '.join(chain[1:]) or 'none'}) "
                        "are also out of daily quota.")


def _chat_one(messages, retries, model, **options):
    """One model: handles Groq rate limits (HTTP 429). A daily limit raises
    _DailyLimitError at once; a per-minute limit waits for Groq's retry-after
    (capped at 30 s) and retries."""
    import time
    from openai import APIConnectionError, APIStatusError, RateLimitError
    for attempt in range(retries):
        try:
            return _client.chat.completions.create(model=model, messages=messages, **options)
        except APIConnectionError as e:  # network dropped (Wi-Fi blip, PC went to sleep)
            if attempt >= 2:
                raise LLMQuotaError("Could not reach the Groq API - the network connection "
                                    "dropped. Check the connection and try again.") from e
            time.sleep(3)
        except RateLimitError as e:  # 429 - must come before APIStatusError (its parent)
            daily = _quota_message(e)
            if daily:
                raise _DailyLimitError(daily) from e
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
              "edge case. Format clearly with headings, each starting 'Test Case <n>'.\n"
              "Each test case covers exactly ONE scenario: one sequence of steps with one "
              "expected outcome. Never put 'Scenario A / Scenario B' or several alternative "
              "inputs into the same test case.\n"
              "SCOPE: every test case must test the requested area and nothing else. The "
              "context may contain sections about other features - ignore those sections "
              "completely. Never write a test case for a different feature, and never add "
              "placeholder or 'insufficient context' test cases.")
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

_SCOPE_STOPWORDS = {
    "test", "tests", "testing", "tested", "case", "cases", "functionality", "function",
    "feature", "features", "flow", "flows", "scenario", "scenarios", "verify", "check",
    "validate", "that", "this", "with", "from", "into", "page", "pages", "site", "website",
    "application", "user", "users", "should", "work", "works", "working", "using", "the",
    "and", "for", "http", "https", "www", "com",
}
# other spellings of a keyword that must count as the same thing
_SCOPE_ALIASES = {
    "login": ("log in", "logs in", "logged in", "logging in", "sign in", "signed in",
              "authenticat", "credential", "password", "username", "secure area"),
    "logout": ("log out", "logs out", "logged out", "sign out"),
}


def _scope_keywords(query):
    """Primary intent keywords: the query's meaningful words, without URLs or filler."""
    import re as _re
    text = _re.sub(r"https?://\S+", " ", query.lower())
    words = [w for w in _re.findall(r"[a-z]{3,}", text) if w not in _SCOPE_STOPWORDS]
    return list(dict.fromkeys(words))


def _scope_filter(query, test_cases):
    """Deterministic scope filter: drop generated test cases that never mention any of the
    intent's keywords (e.g. a meter-reading case for a 'login' intent).
    Returns (kept_text, dropped_titles). Fails open: if nothing could be matched, or every
    case would be dropped, the text is returned unchanged."""
    keywords = _scope_keywords(query)
    cases = split_test_cases(test_cases)
    if not keywords or len(cases) < 2:
        return test_cases, []

    terms = []
    for word in keywords:
        terms.append(word[:6] if len(word) > 6 else word)  # cancellation -> cancel
        terms.extend(_SCOPE_ALIASES.get(word, ()))

    def in_scope(case_text):
        # The title decides. A passing mention in the body is not enough (an order test
        # may start with "Log in as a CSR"), so the body needs at least 3 mentions.
        low = case_text.lower()
        lines = [ln for ln in low.splitlines() if ln.strip()]
        title = " ".join(lines[:1] + [ln for ln in lines[1:4] if "title" in ln])
        if any(term in title for term in terms):
            return True
        return sum(low.count(term) for term in terms) >= 3

    kept = [c for c in cases if in_scope(c)]
    if not kept or len(kept) == len(cases):
        return test_cases, []
    dropped = [_case_title(c) for c in cases if not in_scope(c)]
    return "\n\n---\n\n".join(kept), dropped


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
    kept, dropped = _scope_filter(query, final["test_cases"])
    return {
        "query": query,
        "context": "\n\n".join(final["context"]),
        "test_cases": kept,
        "out_of_scope_dropped": dropped,
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

def _load_selenium_rag(name):
    """Load a module from the selenium-rag folder by path (the folder name has a hyphen,
    and its module names - config, guardrails - are too generic to put on sys.path)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selenium-rag", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"selenium_rag_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Reused from selenium-rag (both dependency-free): static analysis + URL whitelist, and the
# exception -> hint map for the repair prompt. Its SeleniumExecutor is NOT used here: it
# exec()s generated code in-process and re-runs AI repairs without human approval.
_sel_guardrails = _load_selenium_rag("guardrails")
_sel_errors = _load_selenium_rag("selenium_errors")

_GENERATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_tests")
_MATCH_THRESHOLD = 7
_EXEC_TIMEOUT = 180

# script_id -> {kind: "playwright"|"selenium", query, requirement, code, sha, path,
#               guardrails, approval, execution, case_scores}
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
        "- SCOPE: write exactly one test function per given test case and nothing else - no tests "
        "for other features, no placeholder, skipped or 'not implemented' test functions\n"
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
    """Ask the LLM for a script and strip markdown fences.

    Groq caps output at 3,072 tokens by default and the model's hidden reasoning counts
    against it, which silently truncated longer scripts. Ask for as much output as fits
    under the free tier's 8,000 tokens/minute (prompt + output), and if the answer is
    still empty or cut off (finish_reason == 'length'), retry once with low reasoning."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    prompt_estimate = (len(system) + len(user)) // 3  # deliberately high (~4 chars/token)
    budget = max(3072, 7800 - prompt_estimate)
    for effort in (None, "low"):
        options = {"temperature": 0, "max_completion_tokens": budget}
        if effort:
            options["reasoning_effort"] = effort
        choice = _chat(messages, **options).choices[0]
        code = re.sub(r"^```[a-zA-Z]*\n?", "", (choice.message.content or "").strip())
        code = re.sub(r"\n?```$", "", code.strip()).strip()
        if code and choice.finish_reason != "length":
            return code
    raise RuntimeError("The LLM's script came back empty or cut off (Groq free-tier output "
                       "limit). Try again, or shorten the App UI details.")


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
        _scripts[script_id] = {"kind": "playwright",
                               "query": query, "test_cases": test_cases, "url": url,
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
    # 1. a UI map bundled with this project for a known host (ui_maps/<host>.txt)
    host = (parts.hostname or "").lower()
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_maps", f"{host}.txt")
    if re.fullmatch(r"[a-z0-9.-]+", host) and os.path.isfile(bundled):
        with open(bundled, "r", encoding="utf-8") as f:
            return {"ui_details": f.read(20000), "source": f"bundled map ui_maps/{host}.txt"}
    # 2. the app's own /ui-map page (the mock CRM serves one)
    map_url = f"{parts.scheme}://{parts.netloc}/ui-map"
    try:
        with urlopen(map_url, timeout=5) as r:
            return {"ui_details": r.read(20000).decode("utf-8", "replace"), "source": map_url}
    except Exception as e:
        return {"error": f"Could not load {map_url}: {e}. Paste your app's UI details instead."}


def url_in_text(text):
    """First http(s) URL mentioned in free text (e.g. inside the intent), or ''."""
    m = re.search(r"https?://[^\s\"'<>)]+", text or "")
    return m.group(0).rstrip(".,;") if m else ""


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


_DRIVER_NOISE = ("chromedriver!", "KERNEL32!", "ntdll!", "GetHandleVerifier", "(No symbol)",
                 "Symbols not available", "DevTools listening", "Stacktrace:")


def _compact_traceback(output, limit=1500):
    """Keep the script's own frames and the error; drop Playwright/Selenium library
    internals and chromedriver's native stack dump."""
    kept, skip = [], False
    for line in output.splitlines():
        if line.startswith('  File "'):
            skip = "site-packages" in line
        elif not line.startswith(" "):
            skip = False
        if not skip and line.strip() and not any(n in line for n in _DRIVER_NOISE):
            kept.append(line)
    return "\n".join(kept)[-limit:]


def _tool(rec):
    return "Selenium" if rec.get("kind") == "selenium" else "Playwright"


def _rules_for(rec):
    if rec.get("kind") == "selenium":
        return _selenium_rules(bool(rec["ui_details"]), rec["visible"])
    return _script_rules(rec["visible"])


def _save_for(rec, code):
    prefix = "selenium " if rec.get("kind") == "selenium" else ""
    return _save_script(prefix + rec["query"], code)


def _rebuild_for(rec, test_cases):
    """Regenerate the record's script (same tool) from new test cases. Returns (code, path)."""
    build = _build_selenium_script if rec.get("kind") == "selenium" else _build_script
    return build(rec["query"], test_cases, rec["url"], rec["visible"], rec["ui_details"])


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
    static_errors = (guard.get("api_errors") or []) + (guard.get("url_errors") or [])
    if static_errors:
        failure = (f"The static {_tool(rec)} checks blocked the script:\n"
                   + "\n".join(static_errors))
    elif run and not run.get("passed"):
        failure = "Running the script failed with this output:\n" + _compact_traceback(run["output"])
        if rec.get("kind") == "selenium":  # hint map reused from selenium-rag
            name, hint = _sel_errors.hint_for(failure)
            failure += f"\n\nHINT{' for ' + name if name else ''}: {hint}"
    else:
        return {"error": "Nothing to fix: the script has not failed a static check or execution."}
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
        code = _llm_code(_rules_for(rec), user)
    except RuntimeError as e:
        return {"error": str(e)}
    path = _save_for(rec, code)
    with _scripts_lock:
        rec.update({"code": code, "sha": _sha(code), "path": path, "guardrails": None,
                    "approval": None, "execution": None, "repairs": rec.get("repairs", 0) + 1})
    return {"script_id": script_id, "code": code, "path": path, "repairs": rec["repairs"],
            "max_repairs": _MAX_REPAIRS, "fixed_failure": failure[:600]}


def _requirement_match(rec):
    """Guardrail 2 - LLM judge: how well does the script test the requirement? (score, reason)"""
    if rec.get("kind") != "selenium":
        # Stage 5's judge; it calls _ask, which retries per-minute rate limits
        return guardrail_match(rec["requirement"], rec["code"])
    system = ("Score 0-10 how well this Selenium script tests the requirement. "
              "Respond EXACTLY:\nSCORE: <n>\nREASON: <one line>")
    # temperature 0: the same script should get the same verdict, especially near the threshold
    result = _ask(system, f"REQUIREMENT:\n{rec['requirement']}\n\nSCRIPT:\n{rec['code']}",
                  temperature=0)
    score, reason = 0, ""
    for line in result.splitlines():
        if line.upper().startswith("SCORE:"):
            digits = "".join(c for c in line.split("/")[0] if c.isdigit())
            score = int(digits) if digits else 0
        if line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return min(score, 10), reason


def run_guardrails(script_id):
    rec = _scripts.get(script_id)
    if not rec:
        return {"error": "Unknown script. Generate the Playwright script again."}
    code, selenium = rec["code"], rec.get("kind") == "selenium"
    safe, hits = guardrail_safety(code)
    url_errors = []
    if selenium:  # layers 1 and 2 reused from selenium-rag/guardrails.py
        ok, violations = _sel_guardrails.layer1_static_analysis(code)
        safe, hits = safe and ok, hits + violations
        _, url_errors = _sel_guardrails.layer2_url_whitelist(code)
    if not safe:
        api_errors = []
    else:
        api_errors = check_selenium_api(code) if selenium else check_playwright_api(code)
    score, reason = (0, "Skipped - safety scan failed.")
    if api_errors:
        score, reason = (0, f"Skipped - script uses {_tool(rec)} APIs that do not exist.")
    elif url_errors:
        score, reason = (0, "Skipped - script opens a URL outside the whitelist.")
    if safe and not api_errors and not url_errors:
        try:
            score, reason = _requirement_match(rec)
        except LLMQuotaError as e:
            return {"error": str(e)}
    result = {"kind": rec.get("kind", "playwright"), "safe": safe, "hits": hits,
              "api_errors": api_errors, "url_errors": url_errors,
              "allowed_domains": list(_sel_guardrails.ALLOWED_DOMAINS) if selenium else [],
              "match_score": score, "match_reason": reason, "threshold": _MATCH_THRESHOLD,
              "passed": (safe and not api_errors and not url_errors
                         and score >= _MATCH_THRESHOLD)}
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


class JudgeJSONError(RuntimeError):
    """The evaluation model did not return valid JSON, even after retries."""


_JUDGE_JSON_RETRIES = 2  # retries after the first attempt


def _repair_json(text):
    """Return the JSON object inside the model's answer as valid JSON text, or None.
    Handles markdown fences / prose around the object and trailing commas."""
    import json
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = text[start:end + 1]
    for attempt in (candidate, re.sub(r",\s*([\]}])", r"\1", candidate)):
        try:
            json.loads(attempt)
            return attempt
        except ValueError:
            continue
    return None


def _judge_json(prompt):
    """One DeepEval judge call that must come back as valid JSON.

    DeepEval's metric prompts all ask for a JSON object. Three things made the judge
    model return something unparsable: free-form output (now Groq JSON mode), hidden
    reasoning using up the default 3,072-token output cap (now low reasoning effort and
    a larger cap), and the occasional malformed object (now repaired, else retried).
    Tries 1 + _JUDGE_JSON_RETRIES times, then raises JudgeJSONError so the caller can
    skip that test case instead of crashing."""
    from openai import BadRequestError
    wants_json = "json" in prompt.lower()
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(1 + _JUDGE_JSON_RETRIES):
        options = {"reasoning_effort": "low", "max_completion_tokens": 4000}
        if wants_json and attempt < 2:  # last attempt: plain mode, in case JSON mode is the problem
            options["response_format"] = {"type": "json_object"}
        try:
            text = _chat(messages, model=_JUDGE_MODEL, **options).choices[0].message.content or ""
        except BadRequestError:  # Groq 'json_validate_failed': the model produced invalid JSON
            continue
        if not wants_json:
            return text
        fixed = _repair_json(text)
        if fixed is not None:
            return fixed
    raise JudgeJSONError("The evaluation model returned invalid JSON "
                         f"{1 + _JUDGE_JSON_RETRIES} times in a row.")


def _groq_judge():
    from deepeval.models.base_model import DeepEvalBaseLLM

    class GroqJudge(DeepEvalBaseLLM):
        def __init__(self):
            pass

        def load_model(self):
            return _client

        def generate(self, prompt: str) -> str:
            return _judge_json(prompt)

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
        scored = None  # (case text, entry) of the last attempt that was scored successfully
        for attempt in range(1, _CASE_MAX_ATTEMPTS + 1):
            try:
                if attempt > 1:
                    current = _regenerate_case(rec["query"], context, current, last_reason)
                entry = _score_case(rec["query"], context, current, judge)
            except Exception as e:
                bad_json = isinstance(e, JudgeJSONError) or "invalid JSON" in str(e)
                message = ("Skipped - the evaluation model returned invalid JSON after "
                           f"{1 + _JUDGE_JSON_RETRIES} attempts." if bad_json else str(e)[:300])
                if scored:  # the refined version could not be scored: keep the scored one
                    current, entry = scored[0], dict(scored[1], warning=message)
                else:       # skip this case with a warning; the other cases still get scored
                    entry = {"error": message, "skipped": bad_json, "passed": False}
                if "daily limit" in str(e):
                    quota_error = str(e)
                break
            scored = (current, entry)
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
              "error_count": sum(1 for c in cases if c.get("error")),
              "skipped_count": sum(1 for c in cases if c.get("skipped"))}

    if result["refined_count"]:
        refined_text = "\n\n---\n\n".join(
            c["final_case"] if not c["refined"]
            else f"**Test Case {c['case_number']} (refined)**\n\n{c['final_case']}"
            for c in cases)
        try:
            code, path = _rebuild_for(rec, refined_text)
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


# =====================================================================
# Selenium script generation (POST /generate)
# intent -> RAG + LangGraph test cases -> Python Selenium 4 script.
# Display only: the script is statically checked but never executed here.
# =====================================================================
def _selenium_rules(has_ui_details, visible=False):
    # --disable-http2: on some networks HTTP/2 streams stall for 30 s per page in Chrome
    mode = ("with a visible window", "--start-maximized") if visible else ("headless", "--headless=new")
    launch = (f"- Define make_driver() that starts Chrome {mode[0]} EXACTLY like this and returns "
              "the driver: options = webdriver.ChromeOptions(); "
              f"options.add_argument('{mode[1]}'); options.add_argument('--disable-http2'); "
              "driver = webdriver.Chrome(options=options); driver.set_page_load_timeout(90)\n"
              "- ISOLATION: run every test function in its OWN fresh browser - for each test: "
              "driver = make_driver(), then try: test_x(driver) finally: driver.quit(). Tests "
              "must never share a browser session, so one test's login, logout or data cannot "
              "affect the next\n")
    selectors = (
        "- Use ONLY the selectors, URLs, test users and data listed in the APP UI DETAILS - never "
        "invent selectors - and use the exact message wording they give\n"
        if has_ui_details else
        "- No UI details were supplied: put every locator in a clearly named constant at the top "
        "of the script with a '# TODO: replace with the real selector' comment\n"
    )
    return (
        "You are a test automation engineer. Convert the given test cases into ONE "
        "self-contained Python Selenium 4 script. Requirements:\n"
        "- Imports: from selenium import webdriver; from selenium.webdriver.common.by import By; "
        "from selenium.webdriver.support.ui import WebDriverWait, Select; "
        "from selenium.webdriver.support import expected_conditions as EC\n"
        + launch +
        "- Use the start URL provided; if none is given, define BASE_URL = 'http://localhost:8000' "
        "at the top with a TODO comment\n"
        + selectors +
        "- Implement each test case as its own function taking the driver\n"
        "- SCOPE: write exactly one test function per given test case and nothing else - no tests "
        "for other features, no placeholder, skipped or 'not implemented' test functions\n"
        "- Each test function performs ONE scenario (one submit, one expected outcome) with the "
        "exact input data that scenario needs; double-check the typed values against the "
        "scenario (e.g. a 'valid username, wrong password' test must type the VALID username)\n"
        "- Text that may carry extra characters (e.g. a flash message with a close icon): check "
        "that it CONTAINS the expected message instead of comparing for equality\n"
        "- Define this helper (also: from selenium.common.exceptions import TimeoutException) and "
        "use it for EVERY check that an element shows a message, so a failure reports what the "
        "page really showed:\n"
        "def wait_text(driver, by, selector, expected, timeout=20):\n"
        "    try:\n"
        "        WebDriverWait(driver, timeout).until("
        "EC.text_to_be_present_in_element((by, selector), expected))\n"
        "    except TimeoutException:\n"
        "        found = [e.text for e in driver.find_elements(by, selector)]\n"
        "        raise AssertionError(f'Expected {expected!r} in {selector!r} but found {found} "
        "at {driver.current_url}')\n"
        "- Wait with WebDriverWait(driver, 10).until(EC...) before reading or clicking elements; "
        "never use time.sleep\n"
        "- Use ONLY real expected_conditions: presence_of_element_located, "
        "visibility_of_element_located, element_to_be_clickable, text_to_be_present_in_element, "
        "url_contains, url_to_be, staleness_of, invisibility_of_element_located, any_of, all_of. "
        "There is NO EC.or_ / EC.and_ - combine conditions with EC.any_of(...) / EC.all_of(...)\n"
        "- A click that submits a form or follows a link RELOADS the page: elements located before "
        "it become stale, and an element with the same id may already exist on the old page. "
        "After such a click, wait for the NEW state - e.g. WebDriverWait(driver, 10).until("
        "EC.text_to_be_present_in_element((By.CSS_SELECTOR, '#success-message'), 'cancelled')) or "
        "EC.staleness_of(old_element) - and only then locate the element again and read it\n"
        "- Use Select(...) for <select> dropdowns and plain assert statements with helpful messages\n"
        "- Each test function must navigate to the page it needs itself; use unique new IDs so "
        "tests do not collide\n"
        "- Only assert a total count when the UI details state that exact count for that scenario\n"
        "- Keep every string literal, including f-strings, on a single line\n"
        "- Every driver must be closed with driver.quit() in a finally block; print 'TEST PASSED' "
        "at the very end only if all tests ran and all assertions held\n"
        "- Never locate an element before the action that creates it (e.g. a result message "
        "that only exists after submitting); wait for it after the action\n"
        "- NO destructive actions (no os, subprocess, shutil, file writes, shell, eval, exec)\n"
        "- Return ONLY runnable code, no markdown fences, no explanation."
    )


def check_selenium_api(code):
    """Static check for a Selenium script: it must parse, and every EC.<condition> and
    By.<STRATEGY> it uses must exist in the installed Selenium (catches invented names
    such as EC.or_). Returns a list of problems; empty means OK."""
    import ast
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Python syntax error on line {e.lineno}: {e.msg}"]
    known = {"EC": expected_conditions, "By": By}
    unknown = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in known and not hasattr(known[node.value.id], node.attr)):
            unknown.add(f"{node.value.id}.{node.attr}")
    return [f"{name} does not exist in Selenium" for name in sorted(unknown)]


def _build_selenium_script(query, test_cases, url, visible, ui_details=""):
    """LLM-generate the Selenium script for the test cases and save it. Returns (code, path)."""
    user = f"Requirement area: {query}\n"
    if url:
        user += f"Start URL: {url}\n"
    user += f"\nTEST CASES:\n{test_cases}\n\n"
    if ui_details:
        user += f"APP UI DETAILS (pages, selectors, test data):\n{ui_details}\n\n"
    user += "Generate the full runnable Selenium script."
    code = _llm_code(_selenium_rules(bool(ui_details), visible), user)
    return code, _save_script(f"selenium {query}", code)


def generate_selenium_script(intent, url="", ui_details="", visible=False):
    """Run the RAG + LangGraph pipeline for the intent, then turn the resulting test cases
    into a Selenium script. The script is registered like a Playwright one, so it goes
    through the same guardrails -> Stage 6 human approval -> execution -> AI repair flow."""
    mark = len(_models_used)
    url = url or url_in_text(intent)          # "Test login on https://..." carries its own URL
    ui_source = "provided" if ui_details else ""
    if url and not ui_details:                # no UI details typed in: look for a UI map
        found = fetch_ui_map(url)
        ui_details, ui_source = found.get("ui_details", ""), found.get("source", "")
    cases = generate_test_cases(intent)
    if "error" in cases:
        return cases
    try:
        code, path = _build_selenium_script(intent, cases["test_cases"], url, visible, ui_details)
    except RuntimeError as e:  # includes LLMQuotaError
        return {"error": str(e)}
    script_id = uuid.uuid4().hex
    with _scripts_lock:
        _scripts[script_id] = {"kind": "selenium", "query": intent,
                               "test_cases": cases["test_cases"], "url": url,
                               "visible": visible, "ui_details": ui_details,
                               "requirement": _requirement(intent, url, cases["test_cases"]),
                               "code": code, "sha": _sha(code), "path": path,
                               "guardrails": None, "approval": None, "execution": None,
                               "case_scores": None}
    return {"script_id": script_id, "intent": intent, "code": code, "path": path,
            "test_cases": cases["test_cases"], "context": cases["context"],
            "score": cases["score"], "attempts": cases["attempts"],
            "feedback": cases["feedback"], "used_ui_details": bool(ui_details),
            "url": url, "ui_details": ui_details, "ui_source": ui_source,
            "out_of_scope_dropped": cases.get("out_of_scope_dropped", []),
            "models": models_used_since(mark), "primary_model": _MODEL}
