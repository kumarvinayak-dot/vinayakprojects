# Agentic AI Test-Case Generator

An AI-powered test-case generation and validation module, built as a cleanly-separated
feature inside an existing enterprise QA automation tool. It combines **RAG**,
**LangGraph** agentic orchestration, and **DeepEval** industry-standard evaluation to
generate requirements-grounded test cases that are automatically scored and self-corrected.

> Built as an extension to a production-style Build Verification & Environment Validation
> tool (ASCF/CRM/AMSS/UXF/OMNI/DEX), demonstrating both **deterministic QA automation** and
> **AI-augmented quality engineering** in one application.

---

## What it does

Given a requirement area (e.g. *"order cancellation"*), the system:

1. **Retrieves** the most relevant requirements from a knowledge base (semantic search)
2. **Generates** structured test cases grounded in those requirements
3. **Validates** the output with an LLM-as-judge, scoring quality and coverage
4. **Self-corrects** — if the score is below threshold, it refines and regenerates (up to 3 attempts)
5. **Scores** faithfulness and answer-relevancy using DeepEval's research-backed metrics

The result: test cases that are grounded (not hallucinated), quality-checked, and traceable
to source requirements — with a transparent score and reasoning.

---

## Architecture

```
        ┌──────────────────────────────────────────────────────┐
        │                  Flask Web UI (/ai-testgen)          │
        │        query box → button → results (score, cases)   │
        └───────────────────────────┬──────────────────────────┘
                                     │
        ┌───────────────────────────▼──────────────────────────┐
        │                    ai_testgen module                  │
        │                                                        │
        │   RAG layer                                            │
        │   ┌────────────┐   ┌──────────┐   ┌─────────────────┐ │
        │   │  chunk +   │──▶│  FAISS   │──▶│ semantic         │ │
        │   │  embed     │   │  vector  │   │ retrieval (top-k)│ │
        │   │ (MiniLM)   │   │  index   │   │                  │ │
        │   └────────────┘   └──────────┘   └────────┬────────┘ │
        │                                            │          │
        │   LangGraph agent loop                     ▼          │
        │   ┌──────────┐    ┌──────────┐    ┌──────────────┐   │
        │   │ GENERATE │───▶│ VALIDATE │───▶│  score >= 8 ? │   │
        │   │(grounded)│    │(LLM judge)│    └──────┬───────┘   │
        │   └────▲─────┘    └──────────┘   yes│      │no        │
        │        │                       DONE │      │          │
        │        └────────── refine ──────────┼──────┘          │
        │                                     │                 │
        │   DeepEval evaluation               ▼                 │
        │   Faithfulness · Answer Relevancy (0-1 + reasons)     │
        └──────────────────────────────────────────────────────┘
                                     │
                        LLM: Groq (openai/gpt-oss-120b)
```

---

## Tech stack

| Layer | Technology |
|---|---|
| LLM | Groq API (`openai/gpt-oss-120b`), OpenAI-compatible client |
| Embeddings | `sentence-transformers` (all-MiniLM-L6-v2, 384-dim, local) |
| Vector store | FAISS (L2 similarity search) |
| Orchestration | LangGraph (stateful generate→validate→refine graph) |
| Evaluation | DeepEval (Faithfulness, Answer Relevancy — custom Groq judge) |
| UI / server | Flask (integrated route + template) |

---

## Key design decisions

- **Grounding prevents hallucination.** Generation is constrained to retrieved context via
  the system prompt, so test cases trace to real requirements rather than model invention.
- **LLM-as-judge for validation.** Rather than trusting a model's self-reported confidence
  (which is unreliable), the validate node scores output *against the retrieved context* —
  faithfulness to evidence, not self-certainty.
- **Bounded self-correction.** The refine loop is capped at 3 attempts to guarantee
  termination while allowing quality improvement.
- **Provider abstraction.** The LLM is accessed through an OpenAI-compatible interface, so
  swapping providers (the project was migrated from GitHub Models to Groq mid-build) required
  only a base-URL and key change, not a rewrite.
- **Separation of concerns.** The AI feature is a standalone module with its own route,
  added without modifying the existing deterministic comparison engine.

---

## How to run

**Prerequisites:** Python 3.12, a Groq API key (free at console.groq.com).

```bash
# 1. Install dependencies
pip install openai sentence-transformers faiss-cpu langchain langgraph deepeval flask

# 2. Set your Groq key (environment variable — never hardcode)
setx GROQ_API_KEY "gsk_yourkey"        # Windows (reopen terminal after)
# export GROQ_API_KEY="gsk_yourkey"    # macOS/Linux

# 3. Ensure crm_requirements.txt is in the app folder, then run
python web_app.py

# 4. Open the AI module
#    http://127.0.0.1:5000/ai-testgen
```

Type a requirement area (`order cancellation`, `proration billing`, `provisioning failure`)
and click **Generate Agentic Test Cases**.

---

## Project structure

```
ai_testgen.py         # RAG + LangGraph + DeepEval pipeline (self-contained module)
crm_requirements.txt  # sample requirements knowledge base
templates/
  ai_testgen.html     # UI page (matches host app styling)
web_app.py            # host Flask app: import + 2 routes added
```

---

## Sample result

For query *"order cancellation"*, the system retrieved the cancellation and modification
requirements, generated three grounded test cases (including edge cases such as blocking
cancellation of *Completed* orders), scored **8/10** after one self-correction, and DeepEval
returned **Faithfulness 1.0** and **Answer Relevancy 1.0** with supporting reasons.

---

## Possible extensions

- Add `HallucinationMetric` and `ContextualRelevancy` for fuller evaluation coverage
- Swap the sample requirements for live document ingestion (PDF/Confluence)
- Calibrate LLM-judge scores against human annotations to surface false positives
- Export generated test cases to a test-management tool (Jira/Zephyr/TestRail)

---

*Built to demonstrate applied AI in quality engineering: RAG grounding, agentic
orchestration, and industry-standard LLM evaluation — integrated into a real QA tool.*
