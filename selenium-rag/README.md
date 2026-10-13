# Selenium RAG Pipeline

**Author:** Vinayak Kumar Srivastava  
**Stack:** Python · Selenium WebDriver · LangGraph · FAISS · DeepEval · Groq LLaMA3

---

## Architecture

```
User Natural Language Intent
        │
        ▼
┌─────────────────────┐
│  RAG Engine         │  FAISS + sentence-transformers
│  (rag_engine.py)    │  Retrieves top-3 Selenium test patterns
└────────┬────────────┘
         │ context
         ▼
┌─────────────────────────────────────────┐
│  LangGraph StateGraph                   │
│  (langgraph_pipeline.py)                │
│                                         │
│  [generate] → [validate] → [refine]     │
│       ↑              │                  │
│       └──────────────┘ (if score < 0.7) │
└────────┬────────────────────────────────┘
         │ generated Selenium code
         ▼
┌─────────────────────┐
│  3-Layer Guardrails │
│  (guardrails.py)    │
│  L1: Static analysis│
│  L2: URL whitelist  │
│  L3: Human approval │
└────────┬────────────┘
         │ approved
         ▼
┌─────────────────────────────────────────┐
│  Selenium Executor                      │
│  (selenium_executor.py)                 │
│                                         │
│  run_test → ✅ PASS                     │
│           → ❌ FAIL → AI Repair Loop    │
│                        (up to 3 times)  │
└─────────────────────────────────────────┘
```

---

## Setup

```bash
# 1. Clone and enter
git clone https://github.com/kumarvinayak-dot/vinayakprojects.git
cd vinayakprojects/selenium-rag

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set Groq API key (NEVER hardcode it)
export GROQ_API_KEY="your_key_here"

# 4. Run
python main.py --demo
# or with a specific intent:
python main.py --intent "login and verify secure area" --demo
```

---

## Files

| File | Purpose |
|------|---------|
| `config.py` | Central config — model, thresholds, Selenium settings |
| `rag_engine.py` | FAISS index build + retrieval of Selenium test patterns |
| `langgraph_pipeline.py` | LangGraph generate→validate→refine state machine |
| `selenium_executor.py` | Execute tests + AI Repair Loop with error hints |
| `guardrails.py` | 3-layer safety (static analysis, URL whitelist, human gate) |
| `main.py` | Orchestrator — ties all components together |

---

## Supported Test Intents (Knowledge Base)

| ID | Intent |
|----|--------|
| login | Login with username and password |
| checkbox | Verify checkbox can be checked/unchecked |
| dropdown | Select option from dropdown |
| dynamic_content | Handle dynamic element with wait |
| iframe | Switch to iframe and interact |
| alert | Handle JS alert popup |
| window | Switch between browser windows |
| table | Read and verify HTML table data |
| hover | Hover to reveal hidden element |
| file_upload | Upload file via input element |

---

## AI Repair Loop — Exception Hints

| Exception | Auto-hint applied |
|-----------|------------------|
| NoSuchElementException | Check locator strategy |
| TimeoutException | Increase wait or fix selector |
| StaleElementReferenceException | Re-fetch element after DOM change |
| ElementNotInteractableException | Scroll into view or wait for visibility |
| ElementClickInterceptedException | Close overlays or use JS click |
| NoAlertPresentException | Add `EC.alert_is_present()` wait |

---

## Interview Talking Points

> "I built a RAG pipeline where natural language test intents are embedded using sentence-transformers and stored in FAISS. On query, the top-3 similar patterns are retrieved and fed into a LangGraph StateGraph with three nodes — generate, validate, and refine. DeepEval checks Faithfulness and Answer Relevancy against a 0.7 threshold. Before execution, three guardrail layers run: static code analysis for dangerous patterns, URL whitelist enforcement, and a mandatory human approval gate. Selenium WebDriver then executes the test, and on failure, an AI Repair Loop maps the exception type to a specific hint and asks the LLM to fix the code — up to three attempts."
