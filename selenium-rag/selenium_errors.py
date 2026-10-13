"""
selenium_errors.py — Selenium exception → repair hint mapping
Author: Vinayak Kumar Srivastava

Dependency-free (standard library only) so both selenium_executor.py and the web
app (ai_testgen.py) can use the same hints in their AI repair prompts.
"""

SELENIUM_ERROR_MAP = {
    "NoSuchElementException":            "Element not found — check locator (ID/CSS/XPATH). Try a more specific selector.",
    "TimeoutException":                  "Element not visible/clickable in time — increase WebDriverWait timeout or check selector.",
    "StaleElementReferenceException":    "DOM refreshed after element was found — re-fetch element after page change or AJAX call.",
    "ElementNotInteractableException":   "Element found but not interactable — scroll into view, wait for visibility, or check if it's hidden.",
    "ElementClickInterceptedException":  "Another element is overlapping the target — scroll, close overlays, or use JS click.",
    "NoAlertPresentException":           "Expected alert not present — add wait for alert: WebDriverWait(driver, 5).until(EC.alert_is_present())",
    "WebDriverException":                "WebDriver crashed or browser closed unexpectedly — restart driver session.",
}

DEFAULT_HINT = "Review the error traceback carefully and fix the root cause."

# Chrome sometimes reports a stale element as a generic WebDriverException; the message
# text identifies the real cause.
MESSAGE_ALIASES = {
    "does not belong to the document": "StaleElementReferenceException",
    "stale element reference":         "StaleElementReferenceException",
}


def hint_for(error_text: str) -> tuple[str, str]:
    """Return (exception name, hint) for the known Selenium exception named LAST in the
    error text — in a traceback the exception actually raised comes at the end.
    Returns ("", DEFAULT_HINT) when none is recognised."""
    for fragment, name in MESSAGE_ALIASES.items():
        if fragment in error_text:
            return name, SELENIUM_ERROR_MAP[name]
    positions = [(error_text.rfind(name), name) for name in SELENIUM_ERROR_MAP if name in error_text]
    if not positions:
        return "", DEFAULT_HINT
    name = max(positions)[1]
    return name, SELENIUM_ERROR_MAP[name]
