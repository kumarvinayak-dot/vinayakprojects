"""
main.py — Selenium RAG Pipeline orchestrator
Author: Vinayak Kumar Srivastava

Full pipeline:
  User Intent
    → RAG retrieval (FAISS + sentence-transformers)
    → LangGraph StateGraph (generate → validate → refine)
    → 3-layer Guardrails
    → Human Approval
    → Selenium execution
    → AI Repair Loop
    → Result

Usage:
  python main.py
  python main.py --intent "login with username tomsmith"
  python main.py --demo   (skips human approval prompt)
"""

import argparse
import sys
from rag_engine import RAGEngine
from langgraph_pipeline import build_pipeline
from guardrails import Guardrails
from selenium_executor import SeleniumExecutor


DEMO_INTENTS = [
    "login with username tomsmith and verify secure area",
    "select option from dropdown",
    "handle javascript alert and accept it",
    "switch to iframe and type text",
    "hover over element to reveal caption",
]


def run_pipeline(intent: str, demo_mode: bool = False) -> dict:
    print("\n" + "="*60)
    print(f"🚀 SELENIUM RAG PIPELINE")
    print(f"   Intent: {intent}")
    print("="*60)

    # ── Step 1: RAG retrieval ──────────────────────────────────────────────
    print("\n[STEP 1] RAG retrieval...")
    engine = RAGEngine()
    rag_context = engine.retrieve(intent)
    print(f"[STEP 1] Retrieved {len(rag_context)} patterns: {[d['id'] for d in rag_context]}")

    # ── Step 2: LangGraph pipeline ─────────────────────────────────────────
    print("\n[STEP 2] LangGraph: generate → validate → refine...")
    pipeline = build_pipeline()
    state = pipeline.invoke({
        "intent":            intent,
        "rag_context":       rag_context,
        "generated_code":    "",
        "validation_score":  0.0,
        "validation_passed": False,
        "repair_attempts":   0,
        "final_code":        "",
        "error_message":     ""
    })

    generated_code = state.get("final_code") or state.get("generated_code")
    validation_passed = state.get("validation_passed", False)

    print(f"[STEP 2] Validation passed: {validation_passed} | Score: {state.get('validation_score', 0):.2f}")

    if not generated_code:
        print("[STEP 2] ❌ No code generated. Aborting.")
        return {"status": "ABORTED", "reason": "Code generation failed"}

    # ── Step 3: Guardrails ─────────────────────────────────────────────────
    print("\n[STEP 3] Guardrails check...")
    guardrails = Guardrails()
    try:
        guardrails.check(intent, generated_code, skip_human=demo_mode)
    except (ValueError, PermissionError) as e:
        return {"status": "BLOCKED", "reason": str(e)}

    # ── Step 4: Selenium execution + AI repair ─────────────────────────────
    print("\n[STEP 4] Executing Selenium test...")
    executor = SeleniumExecutor()
    result = executor.execute(intent, generated_code)

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("📊 PIPELINE RESULT")
    print("="*60)
    print(f"Status   : {result['status']}")
    print(f"Attempts : {result['attempts']}")
    print(f"Validated: {validation_passed}")
    if result["error"]:
        print(f"Final error: {result['error'].splitlines()[0]}")
    print("="*60)

    return result


def main():
    parser = argparse.ArgumentParser(description="Selenium RAG Pipeline")
    parser.add_argument("--intent", type=str, help="Natural language test intent")
    parser.add_argument("--demo",   action="store_true", help="Skip human approval prompt")
    args = parser.parse_args()

    if args.intent:
        run_pipeline(args.intent, demo_mode=args.demo)
    else:
        # Interactive mode
        print("Selenium RAG Pipeline — Interactive Mode")
        print("Demo intents:")
        for i, d in enumerate(DEMO_INTENTS, 1):
            print(f"  {i}. {d}")
        print()
        choice = input("Enter intent (or pick 1-5): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(DEMO_INTENTS):
            intent = DEMO_INTENTS[int(choice) - 1]
        else:
            intent = choice
        run_pipeline(intent, demo_mode=True)


if __name__ == "__main__":
    main()
