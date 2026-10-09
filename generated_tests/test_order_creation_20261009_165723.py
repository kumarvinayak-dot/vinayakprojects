from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
BASE_URL = "http://127.0.0.1:5050"

def login(page, username, password, expected_display_name):
    page.goto(START_URL)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#login-button")
    expect(page.locator("#current-user")).to_have_text(expected_display_name)

def navigate_to_create_order(page):
    # Direct navigation is reliable
    page.goto(f"{BASE_URL}/orders/new")

def submit_order(page, order_id, account_value, product_value, quantity):
    if order_id:
        page.fill("#order-id-input", order_id)
    page.select_option("#customer-account", account_value)
    page.select_option("#product-1", product_value)
    page.fill("#quantity-1", str(quantity))
    page.click("#submit-order")

def test_successful_order_creation(browser):
    page = browser.new_page()
    login(page, "csr1", "Password123!", "Asha (CSR)")
    navigate_to_create_order(page)

    order_id = "ORD-20231001-001"
    submit_order(page, order_id, "ACC-1001", "FIBER-100", 2)

    # Success message
    expect(page.locator("#success-message")).to_have_text(
        f"Order {order_id} created successfully with status New."
    )
    # Order detail assertions
    expect(page.locator("#order-id")).to_have_text(order_id)
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")
    # Verify line item quantity
    qty_locator = page.locator("#line-items .line-item").first.locator(".line-qty")
    expect(qty_locator).to_have_text("2")
    page.close()

def test_rejection_for_suspended_customer(browser):
    page = browser.new_page()
    login(page, "csr1", "Password123!", "Asha (CSR)")
    navigate_to_create_order(page)

    order_id = "ORD-20231001-002"
    submit_order(page, order_id, "ACC-2001", "FIBER-500", 1)

    # Error message for suspended account
    expect(page.locator("#error-message")).to_have_text(
        "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
    )
    page.close()

def test_duplicate_order_id(browser):
    page = browser.new_page()
    login(page, "csr1", "Password123!", "Asha (CSR)")

    # First creation (should succeed)
    duplicate_id = "ORD-20231001-003"
    navigate_to_create_order(page)
    submit_order(page, duplicate_id, "ACC-1002", "TV-BASIC", 5)
    expect(page.locator("#success-message")).to_have_text(
        f"Order {duplicate_id} created successfully with status New."
    )
    page.goto(f"{BASE_URL}/orders/new")  # go back to create form for duplicate attempt

    # Attempt duplicate
    submit_order(page, duplicate_id, "ACC-1002", "FIBER-100", 1)
    expect(page.locator("#error-message")).to_have_text(
        f"Order ID {duplicate_id} already exists. Please enter a unique Order ID."
    )
    page.close()

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            test_successful_order_creation(browser)
            test_rejection_for_suspended_customer(browser)
            test_duplicate_order_id(browser)
        finally:
            browser.close()
    print("TEST PASSED")

if __name__ == "__main__":
    main()