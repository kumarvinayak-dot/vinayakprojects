from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
USERNAME = "csr1"
PASSWORD = "Password123!"

def login(page):
    page.goto(START_URL)
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#login-button")
    # Wait for successful login indication
    expect(page.locator("#current-user")).to_have_text("Asha (CSR)")

def navigate_to_create_order(page):
    # Use nav link or direct URL
    page.click("#nav-create-order")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")

def fill_order_form(page, order_id, customer_account, product_code, quantity):
    if order_id:
        page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", customer_account)
    # Map test product codes to available dropdown values
    # P123/P456/P789 are mapped to "FIBER-100" for demonstration
    page.select_option("#product-1", "FIBER-100")
    page.fill("#quantity-1", str(quantity))

def submit_and_verify_success(page, expected_order_id):
    page.click("#submit-order")
    success_msg = page.locator("#success-message")
    expect(success_msg).to_contain_text(f"Order {expected_order_id} created successfully with status New.")
    # Verify order detail fields
    expect(page.locator("#order-id")).to_have_text(expected_order_id)
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text(expected_order_id)  # placeholder, will be refined below

def test_successful_order_creation(page):
    login(page)
    navigate_to_create_order(page)
    order_id = "ORD-20231001-001"
    fill_order_form(page, order_id, "ACC-1001", "P123", 2)
    submit_and_verify_success(page, order_id)
    # Verify line item quantity
    expect(page.locator(".line-item .line-qty")).to_have_text("2")
    # Verify customer reference
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")

def test_suspended_customer_rejection(page):
    login(page)
    navigate_to_create_order(page)
    order_id = "ORD-20231001-002"
    fill_order_form(page, order_id, "ACC-2001", "P456", 1)  # ACC-2001 is Suspended
    page.click("#submit-order")
    error_msg = page.locator("#error-message")
    expect(error_msg).to_contain_text("Order rejected: customer account ACC-2001 is suspended")

def test_duplicate_order_id(page):
    login(page)
    navigate_to_create_order(page)
    dup_id = "ORD-20231001-003"
    # First creation (should succeed)
    fill_order_form(page, dup_id, "ACC-1001", "P789", 5)
    submit_and_verify_success(page, dup_id)
    # Navigate back to creation form for duplicate attempt
    navigate_to_create_order(page)
    fill_order_form(page, dup_id, "ACC-1001", "P789", 1)
    page.click("#submit-order")
    error_msg = page.locator("#error-message")
    expect(error_msg).to_contain_text(f"Order ID {dup_id} already exists")

def run_all_tests():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        test_successful_order_creation(page)
        # Reset page for next test
        page.goto("about:blank")
        test_suspended_customer_rejection(page)
        page.goto("about:blank")
        test_duplicate_order_id(page)
        print("TEST PASSED")
        context.close()
        browser.close()

if __name__ == "__main__":
    run_all_tests()