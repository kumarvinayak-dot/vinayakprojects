from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
CSR_USERNAME = "csr1"
CSR_PASSWORD = "Password123!"
SUP_USERNAME = "sup1"
SUP_PASSWORD = "Super123!"

def login(page, username, password):
    page.goto(START_URL)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#login-button")
    # wait for successful login redirect
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    expect(page.locator("#current-user")).to_be_visible()

def open_create_order(page):
    # can use nav link or direct URL
    page.goto("http://127.0.0.1:5050/orders/new")
    expect(page.locator("#order-id-input")).to_be_visible()

def fill_order(page, order_id, account_value, product_code, quantity):
    if order_id:
        page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", account_value)
    page.select_option("#product-1", product_code)
    page.fill("#quantity-1", str(quantity))
    # optional delivery address
    page.fill("#delivery-address", "123 Test St, Test City")
    page.click("#submit-order")

def test_successful_order_creation(browser):
    page = browser.new_page()
    login(page, CSR_USERNAME, CSR_PASSWORD)
    open_create_order(page)

    order_id = "ORD-20231001-001"
    fill_order(page, order_id, "ACC-1001", "FIBER-100", 2)

    # Success message
    success_msg = page.locator("#success-message")
    expect(success_msg).to_have_text(f"Order {order_id} created successfully with status New.")

    # Verify order detail fields
    expect(page.locator("#order-id")).to_have_text(order_id)
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_have_text("ACC-1001")

    # Verify line item quantity
    line_qty = page.locator("#line-items .line-item .line-qty")
    expect(line_qty).to_have_text("2")

    page.close()

def test_rejection_suspended_account(browser):
    page = browser.new_page()
    login(page, CSR_USERNAME, CSR_PASSWORD)
    open_create_order(page)

    order_id = "ORD-20231001-002"
    fill_order(page, order_id, "ACC-2001", "TV-BASIC", 1)

    error_msg = page.locator("#error-message")
    expect(error_msg).to_have_text(
        "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
    )
    page.close()

def test_duplicate_order_id(browser):
    page = browser.new_page()
    login(page, CSR_USERNAME, CSR_PASSWORD)

    # ---- Create the initial order ----
    open_create_order(page)
    dup_order_id = "ORD-20231001-003"
    fill_order(page, dup_order_id, "ACC-1002", "FIBER-500", 1)

    success_msg = page.locator("#success-message")
    expect(success_msg).to_have_text(f"Order {dup_order_id} created successfully with status New.")

    # ---- Attempt to create duplicate ----
    open_create_order(page)
    fill_order(page, dup_order_id, "ACC-1001", "FIBER-100", 1)

    error_msg = page.locator("#error-message")
    expect(error_msg).to_have_text(
        f"Order ID {dup_order_id} already exists. Please enter a unique Order ID."
    )
    page.close()

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        test_successful_order_creation(browser)
        test_rejection_suspended_account(browser)
        test_duplicate_order_id(browser)
        browser.close()
    print("TEST PASSED")

if __name__ == "__main__":
    main()