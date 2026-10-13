"""
selenium_executor.py — Execute generated Selenium tests + AI Repair Loop
Author: Vinayak Kumar Srivastava

Flow:
  1. Run the generated test code via exec()
  2. Capture exceptions with full traceback
  3. Map exception type → human-readable hint
  4. Feed to LLM repair loop (up to MAX_REPAIR_ATTEMPTS)
  5. Screenshot on failure for debugging
"""

import os
import time
import traceback
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
    ElementNotInteractableException,
    ElementClickInterceptedException,
    WebDriverException,
    NoAlertPresentException,
)
from webdriver_manager.chrome import ChromeDriverManager
from langchain_groq import ChatGroq
from langchain.prompts import ChatPromptTemplate
from config import (
    GROQ_API_KEY, GROQ_MODEL,
    SELENIUM_HEADLESS, SELENIUM_IMPLICIT_WAIT, SELENIUM_PAGE_TIMEOUT,
    SCREENSHOT_DIR, MAX_REPAIR_ATTEMPTS, BASE_URL
)
import os
os.environ["GROQ_API_KEY"] = GROQ_API_KEY or ""


# ── Error → hint mapping (shared with the web app, see selenium_errors.py) ────
from selenium_errors import SELENIUM_ERROR_MAP


# ── Driver factory ────────────────────────────────────────────────────────────
def create_driver() -> webdriver.Chrome:
    options = Options()
    if SELENIUM_HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(SELENIUM_IMPLICIT_WAIT)
    driver.set_page_load_timeout(SELENIUM_PAGE_TIMEOUT)
    return driver


# ── Screenshot on failure ─────────────────────────────────────────────────────
def take_screenshot(driver: webdriver.Chrome, name: str) -> str:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOT_DIR, f"{name}_{int(time.time())}.png")
    driver.save_screenshot(path)
    print(f"[SCREENSHOT] Saved: {path}")
    return path


# ── Execute generated code ────────────────────────────────────────────────────
def run_test_code(code: str, driver: webdriver.Chrome) -> tuple[bool, str]:
    """
    Execute the LLM-generated test code in a sandbox namespace.
    Returns (success, error_message).
    """
    namespace = {
        "driver": driver,
        "By": By,
        "WebDriverWait": WebDriverWait,
        "EC": EC,
        "Select": Select,
        "ActionChains": ActionChains,
        "BASE_URL": BASE_URL,
        "time": time,
    }
    try:
        exec(compile(code, "<generated_test>", "exec"), namespace)

        # Find and call the test function (named test_*)
        test_fn = next(
            (v for k, v in namespace.items() if k.startswith("test_") and callable(v)),
            None
        )
        if test_fn:
            test_fn(driver)
        else:
            # If no function defined, code ran inline — treat as success
            pass

        return True, ""

    except AssertionError as e:
        return False, f"AssertionError: {e}\n{traceback.format_exc()}"
    except Exception as e:
        exc_type = type(e).__name__
        return False, f"{exc_type}: {e}\n{traceback.format_exc()}"


# ── AI Repair Loop ────────────────────────────────────────────────────────────
llm = ChatGroq(model=GROQ_MODEL, temperature=0.1)

REPAIR_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a Selenium WebDriver expert fixing a failing test.
Analyze the error, apply the hint, and return corrected Python code only.
Do NOT include any explanation. Return ONLY the fixed Python code."""),
    ("human", """
Intent: {intent}

Failed code:
{code}

Error:
{error}

Hint: {hint}

Fixed code:
""")
])


def repair_code(intent: str, code: str, error: str) -> str:
    exc_type = error.split(":")[0].strip().split("\n")[0]
    hint = SELENIUM_ERROR_MAP.get(exc_type, "Review the error traceback carefully and fix the root cause.")

    print(f"[REPAIR] Exception: {exc_type}")
    print(f"[REPAIR] Hint: {hint}")

    chain = REPAIR_PROMPT | llm
    response = chain.invoke({
        "intent": intent,
        "code":   code,
        "error":  error[:2000],   # truncate very long tracebacks
        "hint":   hint
    })
    fixed = response.content.strip()
    if fixed.startswith("```"):
        lines = fixed.split("\n")
        fixed = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
    return fixed


# ── Main executor ─────────────────────────────────────────────────────────────
class SeleniumExecutor:

    def execute(self, intent: str, code: str) -> dict:
        """
        Run generated test. Repair on failure. Return execution result.
        """
        print(f"\n[EXECUTOR] Running test for: '{intent}'")
        driver = create_driver()
        current_code = code
        attempts = 0

        try:
            while attempts <= MAX_REPAIR_ATTEMPTS:
                success, error = run_test_code(current_code, driver)

                if success:
                    print(f"[EXECUTOR] ✅ Test PASSED on attempt {attempts + 1}")
                    return {
                        "status":   "PASSED",
                        "attempts": attempts + 1,
                        "code":     current_code,
                        "error":    None
                    }

                print(f"[EXECUTOR] ❌ Attempt {attempts + 1} FAILED: {error.splitlines()[0]}")
                take_screenshot(driver, f"failure_attempt_{attempts + 1}")

                if attempts >= MAX_REPAIR_ATTEMPTS:
                    break

                # Ask LLM to repair
                print(f"[EXECUTOR] 🔧 Sending to AI repair loop...")
                current_code = repair_code(intent, current_code, error)
                attempts += 1

            return {
                "status":   "FAILED",
                "attempts": attempts + 1,
                "code":     current_code,
                "error":    error
            }

        finally:
            driver.quit()
            print("[EXECUTOR] Driver closed.")


if __name__ == "__main__":
    # Quick smoke test with a hardcoded snippet
    test_code = """
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def test_login(driver):
    driver.get(BASE_URL + "/login")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "username")))
    driver.find_element(By.ID, "username").send_keys("tomsmith")
    driver.find_element(By.ID, "password").send_keys("SuperSecretPassword!")
    driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".flash.success"))
    )
    msg = driver.find_element(By.CSS_SELECTOR, ".flash.success").text
    assert "You logged into a secure area!" in msg, f"Unexpected message: {msg}"
"""
    executor = SeleniumExecutor()
    result = executor.execute("login with username tomsmith", test_code)
    print("\nResult:", result["status"])
