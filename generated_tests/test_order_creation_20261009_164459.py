from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
USERNAME = "csr1"
PASSWORD = "Password123!"

def login(page):
    page.goto(START_URL)
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#login-button")
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    expect(page.locator("#current-user")).to_have_text("Asha (CSR)")

def navigate_to_create_order(page):
    page.click("#create-order-link")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")

def create_order(page, order_id, customer_account, product_code, quantity):
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", customer_account)
    page.select_option("#product-1", product_code)
    page.fill("#quantity-1", str(quantity))
    page.click("#submit-order")

def test_successful_order_creation(page):
    # Step 1-2: login and navigate
    login(page)
    navigate_to_create_order(page)

    # Step 3-5: fill order details
    create_order(
        page,
        order_id="ORD-20231001-001",
        customer_account="ACC-1001",  # Active account
        product_code="FIBER-100",     # Mapped product
        quantity=2
    )

    # Expected success message
    success_msg = page.locator("#success-message")
    expect(success_msg).to_contain_text(
        "Order ORD-20231001-001 created successfully with status New."
    )

    # Verify order detail fields
    order_id_elem = page.locator("#order-id")
    expect(order_id_elem).to_have_text("ORD-20231001-001")

    status_elem = page.locator("#order-status")
    expect(status_elem).to_have_text("New")

    customer_elem = page.locator("#order-customer")
    expect(customer_elem).to_contain_text("ACC-1001")

    # Verify at least one line item with correct quantity
    line_qty = page.locator(".line-item .line-qty").first
    expect(line_qty).to_have_text("2")

def test_suspended_customer_rejection(page):
    # Ensure we are logged in (reuse session)
    if not page.url.startswith("http://127.0.0.1:5050/orders"):
        login(page)

    navigate_to_create_order(page)

    create_order(
        page,
        order_id="ORD-20231001-002",
        customer_account="ACC-2001",  # Suspended account
        product_code="FIBER-500",
        quantity=1
    )

    error_msg = page.locator("#error-message")
    expect(error_msg).to_contain_text(
        "Order rejected: customer account ACC-2001 is suspended"
    )

def test_duplicate_order_id(page):
    # Ensure logged in
    if not page.url.startswith("http://127.0.0.1:5050/orders"):
        login(page)

    # First creation (should succeed)
    navigate_to_create_order(page)
    create_order(
        page,
        order_id="ORD-20231001-003",
        customer_account="ACC-1002",  # Active account
        product_code="TV-BASIC",
        quantity=5
    )
    success_msg = page.locator("#success-message")
    expect(success_msg).to_contain_text(
        "Order ORD-20231001-003 created successfully with status New."
    )

    # Return to orders list and open create order again
    page.goto("http://127.0.0.1:5050/orders")
    navigate_to_create_order(page)

    # Attempt duplicate
    create_order(
        page,
        order_id="ORD-20231001-003",  # Duplicate ID
        customer_account="ACC-1002",
        product_code="TV-BASIC",
        quantity=5
    )
    error_msg = page.locator("#error-message")
    expect(error_msg).to_contain_text(
        "Order ID ORD-20231001-003 already exists. Please enter a unique Order ID."
    )

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        test_successful_order_creation(page)
        test_suspended_customer_rejection(page)
        test_duplicate_order_id(page)

        print("TEST PASSED")

        context.close()
        browser.close()

if __name__ == "__main__":
    main()