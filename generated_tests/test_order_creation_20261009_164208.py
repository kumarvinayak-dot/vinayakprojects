from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"

def login(page):
    page.goto(START_URL)
    page.fill("#username", "csr1")
    page.fill("#password", "Password123!")
    page.click("#login-button")
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    expect(page.locator("#current-user")).to_contain_text("csr1")

def create_order(page, order_id, account_value, product_value, quantity):
    # open create order form
    page.click("#create-order-link")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")
    # fill form
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", account_value)
    page.select_option("#product-1", product_value)
    page.fill("#quantity-1", str(quantity))
    # submit
    page.click("#submit-order")

def test_successful_order_creation(page):
    login(page)
    create_order(page, "ORD-20231001-001", "ACC-1001", "FIBER-100", 2)
    # verify success message
    success_msg = page.locator("#success-message")
    expect(success_msg).to_have_text("Order ORD-20231001-001 created successfully with status New.")
    # verify order detail fields
    expect(page.locator("#order-id")).to_have_text("ORD-20231001-001")
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")
    # verify line item quantity
    expect(page.locator(".line-item .line-qty")).to_have_text("2")

def test_rejection_for_suspended_account(page):
    login(page)
    create_order(page, "ORD-20231001-002", "ACC-2001", "FIBER-500", 1)
    error_msg = page.locator("#error-message")
    expect(error_msg).to_have_text("Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts.")

def test_duplicate_order_id(page):
    login(page)
    # create initial order
    create_order(page, "ORD-20231001-003", "ACC-1002", "TV-BASIC", 1)
    success_msg = page.locator("#success-message")
    expect(success_msg).to_have_text("Order ORD-20231001-003 created successfully with status New.")
    # navigate back to order creation to attempt duplicate
    page.goto("http://127.0.0.1:5050/orders")
    create_order(page, "ORD-20231001-003", "ACC-1002", "TV-BASIC", 5)
    dup_error = page.locator("#error-message")
    expect(dup_error).to_have_text("Order ID ORD-20231001-003 already exists. Please enter a unique Order ID.")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    test_successful_order_creation(page)
    test_rejection_for_suspended_account(page)
    test_duplicate_order_id(page)
    print("TEST PASSED")
    context.close()
    browser.close()