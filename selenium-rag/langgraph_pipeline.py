"""
langgraph_pipeline.py — LangGraph StateGraph: generate → validate → refine
Author: Vinayak Kumar Srivastava

Nodes:
  generate  → LLM converts RAG context + intent into a Selenium test
  validate  → DeepEval checks Faithfulness + Answer Relevancy
  refine    → If validation fails, LLM refines the test
"""

from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, END
from langchain_groq import ChatGroq
from langchain.prompts import ChatPromptTemplate
from config import GROQ_API_KEY, GROQ_MODEL, FAITHFULNESS_THRESHOLD, ANSWER_RELEVANCY_THRESHOLD
import os

os.environ["GROQ_API_KEY"] = GROQ_API_KEY or ""


# ── State schema ──────────────────────────────────────────────────────────────
class TestState(TypedDict):
    intent:           str           # natural language test intent from user
    rag_context:      list[dict]    # retrieved docs from FAISS
    generated_code:   str           # Selenium test code produced by LLM
    validation_score: float         # DeepEval composite score
    validation_passed: bool
    repair_attempts:  int
    final_code:       str
    error_message:    str


# ── LLM setup ─────────────────────────────────────────────────────────────────
llm = ChatGroq(model=GROQ_MODEL, temperature=0.1)

GENERATE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a Selenium WebDriver expert.
Given a test intent and reference patterns from the knowledge base,
generate a complete, runnable Python Selenium test function.

Rules:
- Use WebDriverWait for all element interactions (no time.sleep)
- Use By.ID, By.CSS_SELECTOR, By.XPATH, By.LINK_TEXT appropriately
- Add clear assert statements
- Target URL: https://the-internet.herokuapp.com
- Include all necessary imports at the top
- Function name must be test_<intent_slug>
- Return ONLY the Python code block, no explanation
"""),
    ("human", """
Intent: {intent}

Reference patterns from knowledge base:
{rag_context}

Generate the Selenium test:
""")
])

REFINE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a Selenium WebDriver expert fixing a failing test.
The validation score was below threshold. Improve the test code.
Make it more robust, accurate, and faithful to the intent.
Return ONLY the corrected Python code."""),
    ("human", """
Original intent: {intent}
Current code: {generated_code}
Validation score: {validation_score}
Issues: {error_message}

Refined test:
""")
])


# ── Node: generate ────────────────────────────────────────────────────────────
def generate_node(state: TestState) -> TestState:
    print(f"\n[GENERATE] Creating Selenium test for: '{state['intent']}'")

    rag_text = "\n\n".join([
        f"Pattern [{i+1}] — {doc['intent']}:\n{doc['code']}"
        for i, doc in enumerate(state["rag_context"])
    ])

    chain = GENERATE_PROMPT | llm
    response = chain.invoke({
        "intent": state["intent"],
        "rag_context": rag_text
    })

    code = response.content.strip()
    # Strip markdown fences if LLM wraps in ```python ... ```
    if code.startswith("```"):
        lines = code.split("\n")
        code = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])

    print(f"[GENERATE] ✅ Code generated ({len(code)} chars)")
    return {**state, "generated_code": code, "repair_attempts": 0}


# ── Node: validate (DeepEval) ─────────────────────────────────────────────────
def validate_node(state: TestState) -> TestState:
    print("[VALIDATE] Running DeepEval metrics...")

    try:
        from deepeval import evaluate
        from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric
        from deepeval.test_case import LLMTestCase

        rag_context_str = " | ".join([d["intent"] for d in state["rag_context"]])

        test_case = LLMTestCase(
            input=state["intent"],
            actual_output=state["generated_code"],
            retrieval_context=[rag_context_str]
        )

        faithfulness = FaithfulnessMetric(threshold=FAITHFULNESS_THRESHOLD, model="gpt-4o-mini")
        relevancy    = AnswerRelevancyMetric(threshold=ANSWER_RELEVANCY_THRESHOLD, model="gpt-4o-mini")

        faithfulness.measure(test_case)
        relevancy.measure(test_case)

        f_score = faithfulness.score
        r_score = relevancy.score
        composite = (f_score + r_score) / 2

        passed = faithfulness.is_successful() and relevancy.is_successful()
        error_msg = ""
        if not passed:
            if not faithfulness.is_successful():
                error_msg += f"Faithfulness {f_score:.2f} < {FAITHFULNESS_THRESHOLD}. "
            if not relevancy.is_successful():
                error_msg += f"Relevancy {r_score:.2f} < {ANSWER_RELEVANCY_THRESHOLD}."

        print(f"[VALIDATE] Faithfulness={f_score:.2f} | Relevancy={r_score:.2f} | Passed={passed}")

    except Exception as e:
        # Fallback: keyword-based heuristic if DeepEval/OpenAI unavailable
        print(f"[VALIDATE] DeepEval unavailable ({e}), using heuristic scoring...")
        code = state["generated_code"]
        score = 0.0
        if "WebDriverWait" in code:       score += 0.2
        if "assert" in code:             score += 0.2
        if "driver.get" in code:         score += 0.2
        if "find_element" in code:       score += 0.2
        if state["intent"].split()[0].lower() in code.lower(): score += 0.2

        composite = score
        passed    = composite >= 0.7
        error_msg = "" if passed else f"Heuristic score {composite:.2f} < 0.7 — code may be incomplete."

    return {
        **state,
        "validation_score": composite,
        "validation_passed": passed,
        "error_message": error_msg,
        "final_code": state["generated_code"] if passed else ""
    }


# ── Node: refine ──────────────────────────────────────────────────────────────
def refine_node(state: TestState) -> TestState:
    attempts = state["repair_attempts"] + 1
    print(f"[REFINE] Attempt {attempts} — score was {state['validation_score']:.2f}")

    chain = REFINE_PROMPT | llm
    response = chain.invoke({
        "intent":           state["intent"],
        "generated_code":   state["generated_code"],
        "validation_score": state["validation_score"],
        "error_message":    state["error_message"]
    })

    code = response.content.strip()
    if code.startswith("```"):
        lines = code.split("\n")
        code = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])

    return {**state, "generated_code": code, "repair_attempts": attempts}


# ── Routing logic ─────────────────────────────────────────────────────────────
def route_after_validate(state: TestState) -> str:
    from config import MAX_REPAIR_ATTEMPTS
    if state["validation_passed"]:
        return "approved"
    if state["repair_attempts"] >= MAX_REPAIR_ATTEMPTS:
        print(f"[ROUTE] Max repair attempts ({MAX_REPAIR_ATTEMPTS}) reached. Escalating to human.")
        return "human_review"
    return "refine"


# ── Build graph ───────────────────────────────────────────────────────────────
def build_pipeline() -> StateGraph:
    graph = StateGraph(TestState)

    graph.add_node("generate", generate_node)
    graph.add_node("validate", validate_node)
    graph.add_node("refine",   refine_node)

    graph.set_entry_point("generate")
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {
            "approved":     END,
            "refine":       "refine",
            "human_review": END,
        }
    )
    graph.add_edge("refine", "validate")

    return graph.compile()


if __name__ == "__main__":
    from rag_engine import RAGEngine
    engine = RAGEngine()

    intent = "login with username tomsmith and verify secure area message"
    context = engine.retrieve(intent)

    pipeline = build_pipeline()
    result = pipeline.invoke({
        "intent":           intent,
        "rag_context":      context,
        "generated_code":   "",
        "validation_score": 0.0,
        "validation_passed": False,
        "repair_attempts":  0,
        "final_code":       "",
        "error_message":    ""
    })

    print("\n" + "="*60)
    print("FINAL GENERATED SELENIUM TEST:")
    print("="*60)
    print(result.get("final_code") or result.get("generated_code"))
