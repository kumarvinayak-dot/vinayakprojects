from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
CSR_USER = "csr1"
CSR_PASS = "Password123!"
SUP_USER = "sup1"
SUP_PASS = "Super123!"

def login(page, username, password):
    page.goto(START_URL)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#login-button")
    # wait for navigation to orders page
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    # ensure logged in
    expect(page.locator("#current-user")).to_be_visible()

def open_create_order(page):
    # can use nav link or direct URL
    page.click("#nav-create-order")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")
    expect(page.locator("#order-id-input")).to_be_visible()

def submit_order(page):
    page.click("#submit-order")

def test_successful_order_creation(page):
    login(page, CSR_USER, CSR_PASS)
    open_create_order(page)

    order_id = "ORD-20231001-001"
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", "ACC-1001")  # Active account
    page.select_option("#product-1", "FIBER-100")        # map P123 -> FIBER-100
    page.fill("#quantity-1", "2")
    submit_order(page)

    # Success message
    success_msg = f"Order {order_id} created successfully with status New."
    expect(page.locator("#success-message")).to_have_text(success_msg)

    # Verify order detail fields
    expect(page.locator("#order-id")).to_have_text(order_id)
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")

def test_rejection_suspended_account(page):
    login(page, CSR_USER, CSR_PASS)
    open_create_order(page)

    order_id = "ORD-20231001-002"
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", "ACC-2001")  # Suspended account
    page.select_option("#product-1", "FIBER-500")        # map P456 -> FIBER-500
    page.fill("#quantity-1", "1")
    submit_order(page)

    error_msg = "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
    expect(page.locator("#error-message")).to_have_text(error_msg)

def create_order_helper(page, order_id, account_value, product_value, quantity):
    open_create_order(page)
    page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", account_value)
    page.select_option("#product-1", product_value)
    page.fill("#quantity-1", str(quantity))
    submit_order(page)
    # wait for success
    expect(page.locator("#success-message")).to_be_visible()
    # return to orders list for next steps
    page.click("#nav-orders")
    expect(page).to_have_url("http://127.0.0.1:5050/orders")

def test_duplicate_order_id(page):
    login(page, CSR_USER, CSR_PASS)

    # Pre‑condition: create an order with the target ID
    dup_id = "ORD-20231001-003"
    create_order_helper(page, dup_id, "ACC-1002", "TV-BASIC", 5)

    # Attempt to create another order with the same ID
    open_create_order(page)
    page.fill("#order-id-input", dup_id)
    page.select_option("#customer-account", "ACC-1001")  # any active account
    page.select_option("#product-1", "ROUTER")           # map P789 -> ROUTER
    page.fill("#quantity-1", "1")
    submit_order(page)

    error_msg = f"Order ID {dup_id} already exists. Please enter a unique Order ID."
    expect(page.locator("#error-message")).to_have_text(error_msg)

def main():
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

if __name__ == "__main__":
    main()