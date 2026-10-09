from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
USERNAME = "csr1"
PASSWORD = "Password123!"

def login(page):
    page.goto(START_URL)
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#login-button")
    # ensure login succeeded
    expect(page.locator("#current-user")).to_have_text("Asha (CSR)")

def test_successful_order_creation(page):
    login(page)
    page.goto("http://127.0.0.1:5050/orders/new")
    order_id = "ORD-20231001-001"
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", "ACC-1001")  # Active account
    page.select_option("#product-1", "FIBER-100")        # Map P123 -> FIBER-100
    page.fill("#quantity-1", "2")
    page.click("#submit-order")
    # Success message
    expect(page.locator("#success-message")).to_contain_text(f"Order {order_id} created successfully with status New.")
    # Verify order details
    expect(page.locator("#order-id")).to_have_text(order_id)
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")
    # Verify line item quantity
    expect(page.locator("#line-items .line-item .line-qty")).to_have_text("2")

def test_rejection_suspended_account(page):
    login(page)
    page.goto("http://127.0.0.1:5050/orders/new")
    order_id = "ORD-20231001-002"
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", "ACC-2001")  # Suspended account
    page.select_option("#product-1", "FIBER-100")        # Map P456 -> FIBER-100
    page.fill("#quantity-1", "1")
    page.click("#submit-order")
    # Error message for suspended account
    expect(page.locator("#error-message")).to_have_text(
        "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
    )

def test_duplicate_order_id(page):
    login(page)
    # First creation (unique)
    page.goto("http://127.0.0.1:5050/orders/new")
    dup_id = "ORD-20231001-003"
    page.fill("#order-id-input", dup_id)
    page.select_option("#customer-account", "ACC-1001")  # Active account
    page.select_option("#product-1", "FIBER-100")        # Map P789 -> FIBER-100
    page.fill("#quantity-1", "1")
    page.click("#submit-order")
    expect(page.locator("#success-message")).to_contain_text(f"Order {dup_id} created successfully with status New.")
    # Attempt duplicate creation
    page.goto("http://127.0.0.1:5050/orders/new")
    page.fill("#order-id-input", dup_id)
    page.select_option("#customer-account", "ACC-1002")  # Another active account
    page.select_option("#product-1", "FIBER-500")
    page.fill("#quantity-1", "3")
    page.click("#submit-order")
    # Duplicate ID error
    expect(page.locator("#error-message")).to_have_text(
        f"Order ID {dup_id} already exists. Please enter a unique Order ID."
    )

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    test_successful_order_creation(page)
    test_rejection_suspended_account(page)
    test_duplicate_order_id(page)
    print("TEST PASSED")
    context.close()
    browser.close()