"""
rag_engine.py — FAISS + sentence-transformers RAG retriever
Author: Vinayak Kumar Srivastava

Stores Selenium test knowledge as embeddings.
On query → retrieves top-K relevant docs → feeds to LangGraph.
"""

from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain.schema import Document
from config import EMBEDDING_MODEL, FAISS_INDEX_PATH, TOP_K_DOCS
import os


# ── Knowledge base: Selenium test patterns ───────────────────────────────────
SELENIUM_KNOWLEDGE = [
    {
        "id": "login",
        "intent": "login to the application with username and password",
        "code": """
def test_login(driver):
    driver.get(BASE_URL + "/login")
    driver.find_element(By.ID, "username").send_keys("tomsmith")
    driver.find_element(By.ID, "password").send_keys("SuperSecretPassword!")
    driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".flash.success"))
    )
    assert "You logged into a secure area!" in driver.find_element(
        By.CSS_SELECTOR, ".flash.success").text
"""
    },
    {
        "id": "checkbox",
        "intent": "verify checkbox can be checked and unchecked",
        "code": """
def test_checkbox(driver):
    driver.get(BASE_URL + "/checkboxes")
    checkboxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
    for cb in checkboxes:
        if not cb.is_selected():
            cb.click()
        assert cb.is_selected(), "Checkbox should be checked"
"""
    },
    {
        "id": "dropdown",
        "intent": "select an option from dropdown",
        "code": """
from selenium.webdriver.support.ui import Select

def test_dropdown(driver):
    driver.get(BASE_URL + "/dropdown")
    select = Select(driver.find_element(By.ID, "dropdown"))
    select.select_by_visible_text("Option 1")
    assert select.first_selected_option.text == "Option 1"
    select.select_by_value("2")
    assert select.first_selected_option.text == "Option 2"
"""
    },
    {
        "id": "dynamic_content",
        "intent": "handle dynamic element that loads after delay",
        "code": """
def test_dynamic_loading(driver):
    driver.get(BASE_URL + "/dynamic_loading/1")
    driver.find_element(By.CSS_SELECTOR, "#start button").click()
    WebDriverWait(driver, 15).until(
        EC.visibility_of_element_located((By.ID, "finish"))
    )
    assert driver.find_element(By.ID, "finish").text == "Hello World!"
"""
    },
    {
        "id": "iframe",
        "intent": "switch to iframe and interact with content inside it",
        "code": """
def test_iframe(driver):
    driver.get(BASE_URL + "/iframe")
    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it((By.ID, "mce_0_ifr"))
    )
    body = driver.find_element(By.TAG_NAME, "body")
    body.clear()
    body.send_keys("Selenium RAG pipeline test")
    driver.switch_to.default_content()
    assert True  # successfully typed in iframe
"""
    },
    {
        "id": "alert",
        "intent": "handle javascript alert popup and accept or dismiss it",
        "code": """
def test_alert(driver):
    driver.get(BASE_URL + "/javascript_alerts")
    driver.find_element(By.XPATH, "//button[text()='Click for JS Alert']").click()
    alert = WebDriverWait(driver, 5).until(EC.alert_is_present())
    assert alert.text == "I am a JS Alert"
    alert.accept()
    result = driver.find_element(By.ID, "result")
    assert result.text == "You successfully clicked an alert"
"""
    },
    {
        "id": "window",
        "intent": "open new window or tab and switch between windows",
        "code": """
def test_multiple_windows(driver):
    driver.get(BASE_URL + "/windows")
    original = driver.current_window_handle
    driver.find_element(By.LINK_TEXT, "Click Here").click()
    WebDriverWait(driver, 5).until(EC.number_of_windows_to_be(2))
    new_window = [w for w in driver.window_handles if w != original][0]
    driver.switch_to.window(new_window)
    assert driver.title == "New Window"
    driver.close()
    driver.switch_to.window(original)
"""
    },
    {
        "id": "table",
        "intent": "read data from html table and verify rows and columns",
        "code": """
def test_table_data(driver):
    driver.get(BASE_URL + "/tables")
    rows = driver.find_elements(By.CSS_SELECTOR, "#table1 tbody tr")
    assert len(rows) > 0, "Table should have at least one row"
    headers = driver.find_elements(By.CSS_SELECTOR, "#table1 thead th")
    header_texts = [h.text for h in headers]
    assert "Last Name" in header_texts
    assert "First Name" in header_texts
"""
    },
    {
        "id": "hover",
        "intent": "hover over element to reveal hidden menu or tooltip",
        "code": """
from selenium.webdriver.common.action_chains import ActionChains

def test_hover(driver):
    driver.get(BASE_URL + "/hovers")
    figures = driver.find_elements(By.CLASS_NAME, "figure")
    actions = ActionChains(driver)
    actions.move_to_element(figures[0]).perform()
    caption = figures[0].find_element(By.CLASS_NAME, "figcaption")
    assert caption.is_displayed(), "Caption should be visible on hover"
"""
    },
    {
        "id": "file_upload",
        "intent": "upload a file using file input element",
        "code": """
def test_file_upload(driver, tmp_path):
    driver.get(BASE_URL + "/upload")
    test_file = tmp_path / "test.txt"
    test_file.write_text("Selenium RAG upload test")
    driver.find_element(By.ID, "file-upload").send_keys(str(test_file))
    driver.find_element(By.ID, "file-submit").click()
    uploaded = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "uploaded-files"))
    )
    assert "test.txt" in uploaded.text
"""
    },
]


class RAGEngine:
    """FAISS-backed retriever for Selenium test patterns."""

    def __init__(self):
        self.embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)
        self.vectorstore = self._build_or_load_index()

    def _build_or_load_index(self) -> FAISS:
        if os.path.exists(FAISS_INDEX_PATH):
            print("[RAG] Loading existing FAISS index...")
            return FAISS.load_local(
                FAISS_INDEX_PATH,
                self.embeddings,
                allow_dangerous_deserialization=True
            )

        print("[RAG] Building FAISS index from knowledge base...")
        docs = [
            Document(
                page_content=item["intent"],
                metadata={"id": item["id"], "code": item["code"]}
            )
            for item in SELENIUM_KNOWLEDGE
        ]
        vectorstore = FAISS.from_documents(docs, self.embeddings)
        vectorstore.save_local(FAISS_INDEX_PATH)
        return vectorstore

    def retrieve(self, query: str) -> list[dict]:
        """Return top-K matching test patterns for a natural language intent."""
        results = self.vectorstore.similarity_search(query, k=TOP_K_DOCS)
        return [
            {
                "intent": doc.page_content,
                "code": doc.metadata["code"],
                "id": doc.metadata["id"],
            }
            for doc in results
        ]


if __name__ == "__main__":
    engine = RAGEngine()
    hits = engine.retrieve("login with username and password")
    for h in hits:
        print(f"✅ Matched: {h['id']} — {h['intent']}")
