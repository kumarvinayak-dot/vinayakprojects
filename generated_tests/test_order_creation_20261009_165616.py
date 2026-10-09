from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
USERNAME = "csr1"
PASSWORD = "Password123!"

def login(page):
    page.goto(START_URL)
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#login-button")
    # wait for navigation to orders page
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    expect(page.locator("#current-user")).to_have_text("Asha (CSR)")

def open_create_order(page):
    # can navigate directly
    page.goto("http://127.0.0.1:5050/orders/new")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")

def submit_order(page):
    page.click("#submit-order")

def test_successful_order_creation(context):
    page = context.new_page()
    login(page)
    open_create_order(page)

    order_id = "ORD-20231001-001"
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", "ACC-1001")  # Active account
    page.select_option("#product-1", "FIBER-100")        # map P123 -> FIBER-100
    page.fill("#quantity-1", "2")
    submit_order(page)

    # Success page assertions
    expect(page.locator("#success-message")).to_have_text(f"Order {order_id} created successfully with status New.")
    expect(page.locator("#order-id")).to_have_text(order_id)
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")
    page.close()

def test_suspended_account_rejection(context):
    page = context.new_page()
    login(page)
    open_create_order(page)

    order_id = "ORD-20231001-002"
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", "ACC-2001")  # Suspended account
    page.select_option("#product-1", "FIBER-500")        # map P456 -> FIBER-500
    page.fill("#quantity-1", "1")
    submit_order(page)

    expect(page.locator("#error-message")).to_have_text(
        "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
    )
    page.close()

def test_duplicate_order_id(context):
    page = context.new_page()
    login(page)

    # ---- Step 1: create the initial order with the target ID ----
    open_create_order(page)
    dup_id = "ORD-20231001-003"
    page.fill("#order-id-input", dup_id)
    page.select_option("#customer-account", "ACC-1002")  # Active account
    page.select_option("#product-1", "TV-BASIC")        # map P789 -> TV-BASIC
    page.fill("#quantity-1", "5")
    submit_order(page)

    # Verify first creation succeeded
    expect(page.locator("#success-message")).to_have_text(f"Order {dup_id} created successfully with status New.")
    page.close()

    # ---- Step 2: attempt to create a second order with the same ID ----
    page2 = context.new_page()
    login(page2)
    open_create_order(page2)

    page2.fill("#order-id-input", dup_id)               # duplicate ID
    page2.select_option("#customer-account", "ACC-1001")  # any active account
    page2.select_option("#product-1", "ROUTER")           # any product
    page2.fill("#quantity-1", "1")
    submit_order(page2)

    expect(page2.locator("#error-message")).to_have_text(
        f"Order ID {dup_id} already exists. Please enter a unique Order ID."
    )
    page2.close()

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        try:
            test_successful_order_creation(context)
            test_suspended_account_rejection(context)
            test_duplicate_order_id(context)
            print("TEST PASSED")
        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()