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
                 api_key=os.environ.get("GROQ_API_KEY"))
_MODEL = "openai/gpt-oss-120b"

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

def _ask(system, user):
    r = _client.chat.completions.create(
        model=_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}])
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
    final = _app_graph.invoke(initial)
    return {
        "query": query,
        "context": "\n\n".join(final["context"]),
        "test_cases": final["test_cases"],
        "score": final["score"],
        "feedback": final["feedback"],
        "attempts": final["attempts"],
    }
