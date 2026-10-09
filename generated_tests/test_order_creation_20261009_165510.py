from playwright.sync_api import sync_playwright, expect

def login(page):
    page.goto("http://127.0.0.1:5050/login")
    page.fill("#username", "csr1")
    page.fill("#password", "Password123!")
    page.click("#login-button")
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    expect(page.locator("#current-user")).to_have_text("Asha (CSR)")

def test_successful_order_creation(browser):
    page = browser.new_page()
    login(page)
    page.goto("http://127.0.0.1:5050/orders/new")
    page.fill("#order-id-input", "ORD-20231001-001")
    page.select_option("#customer-account", "ACC-1001")  # Active account
    page.select_option("#product-1", "FIBER-100")        # Mapped product
    page.fill("#quantity-1", "2")
    page.click("#submit-order")
    expect(page.locator("#success-message")).to_have_text(
        "Order ORD-20231001-001 created successfully with status New."
    )
    expect(page.locator("#order-id")).to_have_text("ORD-20231001-001")
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")
    expect(page.locator(".line-item .line-qty")).to_have_text("2")
    page.close()

def test_rejection_suspended_account(browser):
    page = browser.new_page()
    login(page)
    page.goto("http://127.0.0.1:5050/orders/new")
    page.fill("#order-id-input", "ORD-20231001-002")
    page.select_option("#customer-account", "ACC-2001")  # Suspended account
    page.select_option("#product-1", "FIBER-100")        # Valid product
    page.fill("#quantity-1", "1")
    page.click("#submit-order")
    expect(page.locator("#error-message")).to_have_text(
        "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
    )
    page.close()

def test_duplicate_order_id(browser):
    page = browser.new_page()
    login(page)
    # Create initial order with the target ID
    page.goto("http://127.0.0.1:5050/orders/new")
    page.fill("#order-id-input", "ORD-20231001-003")
    page.select_option("#customer-account", "ACC-1001")  # Active account
    page.select_option("#product-1", "FIBER-100")
    page.fill("#quantity-1", "1")
    page.click("#submit-order")
    expect(page.locator("#success-message")).to_have_text(
        "Order ORD-20231001-003 created successfully with status New."
    )
    # Attempt to create a second order with the same ID
    page.goto("http://127.0.0.1:5050/orders/new")
    page.fill("#order-id-input", "ORD-20231001-003")
    page.select_option("#customer-account", "ACC-1002")  # Another active account
    page.select_option("#product-1", "FIBER-500")
    page.fill("#quantity-1", "5")
    page.click("#submit-order")
    expect(page.locator("#error-message")).to_have_text(
        "Order ID ORD-20231001-003 already exists. Please enter a unique Order ID."
    )
    page.close()

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            test_successful_order_creation(browser)
            test_rejection_suspended_account(browser)
            test_duplicate_order_id(browser)
        finally:
            browser.close()
        print("TEST PASSED")

if __name__ == "__main__":
    main()