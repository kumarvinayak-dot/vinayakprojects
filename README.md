# Vinayak Kumar Srivastava — QA Engineering Portfolio

Senior QA / Test Architect with **18+ years** in enterprise quality engineering, working at
the intersection of **traditional QA automation** and **applied AI in testing**. This
repository contains two working, runnable projects that demonstrate both ends of that spectrum.

📧 kumar.vinayak@gmail.com · 🔗 [LinkedIn](https://www.linkedin.com/in/vinayak-srivastava-89819a19)

---

# 🤖 Project 1 — Agentic AI Test-Case Generator

An AI-powered test-case generation and validation system, built as a cleanly-separated module
inside the QA tool below. It combines **RAG**, **LangGraph** agentic orchestration, and
**DeepEval** industry-standard evaluation to generate requirements-grounded test cases that
are automatically scored and self-corrected — and it runs live in a web UI.

### What it does
Given a requirement area (e.g. *"order cancellation"*), the system:
1. **Retrieves** the most relevant requirements via semantic search (embeddings + FAISS)
2. **Generates** structured test cases grounded strictly in those requirements
3. **Validates** the output with an LLM-as-judge, scoring quality and edge coverage
4. **Self-corrects** — if the score is below threshold, it refines and regenerates (max 3 attempts)
5. **Scores** faithfulness and answer-relevancy using DeepEval's research-backed metrics

### Architecture
```
   Flask UI (/ai-testgen)  →  query box → button → results (score, test cases, context)
          │
   RAG:        chunk + embed (MiniLM) → FAISS index → top-k semantic retrieval
          │
   LangGraph:  GENERATE ──▶ VALIDATE ──▶ score >= 8 ?  ──yes──▶ DONE
                  ▲                            │no
                  └──────── refine ────────────┘   (bounded: max 3 attempts)
          │
   DeepEval:   Faithfulness · Answer Relevancy  (0–1 scores with reasons)
          │
   LLM:        Groq (openai/gpt-oss-120b), OpenAI-compatible client
```

### Tech stack
| Layer | Technology |
|---|---|
| LLM | Groq API (`openai/gpt-oss-120b`) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2, 384-dim, local) |
| Vector store | FAISS (L2 similarity search) |
| Orchestration | LangGraph (stateful generate→validate→refine graph) |
| Evaluation | DeepEval (Faithfulness, Answer Relevancy — custom Groq judge) |
| UI / server | Flask (integrated route + template) |

### Key design decisions
- **Grounding prevents hallucination** — generation is constrained to retrieved context, so
  test cases trace to real requirements rather than model invention.
- **LLM-as-judge for validation** — scores output *against retrieved context* (faithfulness to
  evidence), not the model's unreliable self-reported confidence.
- **Bounded self-correction** — the refine loop is capped to guarantee termination.
- **Provider abstraction** — accessed via an OpenAI-compatible interface, so migrating providers
  (this was moved from GitHub Models to Groq mid-build) needed only a URL + key change.
- **Separation of concerns** — added as a standalone module without modifying the deterministic
  comparison engine of Project 2.

### Run it
```bash
pip install openai sentence-transformers faiss-cpu langchain langgraph deepeval flask
setx GROQ_API_KEY "gsk_yourkey"        # Windows (reopen terminal after); never hardcode
python web_app.py
# open http://127.0.0.1:5000/ai-testgen  → type a requirement area → Generate
```

📁 Detailed write-up: [`README_AI_MODULE.md`](./README_AI_MODULE.md)

---

# 🔍 Project 2 — Build Verification & Environment Validation Tool

A production-style QA automation tool for enterprise CRM releases
(**ASCF / CRM / AMSS / UXF / OMNI / DEX**). It compares old vs new builds, validates JAR
manifest versions across deployment environments over SSH, and generates comparison reports —
all through a Flask web interface.

### What it does
- **Build comparison** — compares previous vs current build paths, flags size mismatches,
  added/removed/changed files, and config differences (CRM / DEX / UXF / WEF)
- **Environment validation** — connects over SSH to deployment servers and verifies JAR
  manifest `Implementation-Version` values against expected builds
- **Reporting** — generates structured HTML comparison reports, browsable in the UI

### Tech stack
Python · Flask · Selenium · Paramiko (SSH) · cryptography · deterministic file/config comparison

### Run it
```bash
pip install -r requirements.txt
python web_app.py
# open http://127.0.0.1:5000/crm
```

📁 Detailed write-up: [`README_WithoutAI_MODULE.md`](./README_WithoutAI_MODULE.md)

---

# What these projects demonstrate together

- **Deterministic QA automation** where reliability and repeatability matter (Project 2)
- **AI-augmented quality engineering** where generation and judgment help (Project 1)
- **Clean architecture** — the AI feature was added as a separate module and route without
  touching the existing comparison engine (separation of concerns)

This reflects how I see modern quality engineering: **traditional automation as the foundation,
with AI applied deliberately where it adds real value.**

---

*18+ years in QA and test engineering (enterprise CRM, billing, telecom BSS), with recent
hands-on work in AI-assisted testing — LLM validation, RAG, and agentic workflows.*
