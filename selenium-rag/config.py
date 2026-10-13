"""
config.py — Central configuration for Selenium RAG Pipeline
Author: Vinayak Kumar Srivastava
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── LLM ──────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")          # never hardcode
GROQ_MODEL   = "llama3-70b-8192"

# ── Embedding / RAG ───────────────────────────────────────────────────────────
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
FAISS_INDEX_PATH = "faiss_index"
TOP_K_DOCS       = 3

# ── DeepEval thresholds ───────────────────────────────────────────────────────
FAITHFULNESS_THRESHOLD    = 0.7
ANSWER_RELEVANCY_THRESHOLD = 0.7

# ── Selenium ──────────────────────────────────────────────────────────────────
SELENIUM_HEADLESS      = True
SELENIUM_IMPLICIT_WAIT = 10          # seconds
SELENIUM_PAGE_TIMEOUT  = 30          # seconds
SCREENSHOT_DIR         = "screenshots"

# ── Repair loop ───────────────────────────────────────────────────────────────
MAX_REPAIR_ATTEMPTS = 3

# ── Target app (the-internet.herokuapp.com — same as SeleniumPractice repo) ──
BASE_URL = "https://the-internet.herokuapp.com"
