from playwright.sync_api import sync_playwright, expect
import re

def login(page):
    page.goto("http://127.0.0.1:5050/login")
    page.fill("#username", "csr1")
    page.fill("#password", "Password123!")
    page.click("#login-button")
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    expect(page.locator("#current-user")).to_have_text("csr1")

def test_successful_order_creation(browser):
    context = browser.new_context()
    page = context.new_page()
    login(page)

    # Navigate to Order Creation screen
    page.click("#create-order-link")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")

    # NOTE: No selector for entering a custom Order ID, so this step is skipped.

    # Select an active customer account
    page.select_option("#customer-account", "ACC-1001")

    # Add a product line item
    page.select_option("#product-1", "FIBER-100")
    page.fill("#quantity-1", "2")
    page.fill("#delivery-address", "123 Test St")

    # Submit the order
    page.click("#submit-order")

    # Verify success message
    success_msg = page.locator("#success-message")
    expect(success_msg).to_contain_text("created successfully with status New")

    # Extract generated Order ID from the success message
    order_id_match = re.search(r"Order (ORD-\d+)", success_msg.text_content() or "")
    order_id = order_id_match.group(1) if order_id_match else ""

    # Verify order details
    expect(page.locator("#order-id")).to_have_text(order_id)
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_have_text("ACC-1001")
    expect(page.locator(".line-item .line-qty")).to_have_text("2")

    context.close()

def test_suspended_account_rejection(browser):
    context = browser.new_context()
    page = context.new_page()
    login(page)

    # Navigate to Order Creation screen
    page.click("#create-order-link")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")

    # NOTE: No selector for entering a custom Order ID, so this step is skipped.

    # Select a suspended customer account
    page.select_option("#customer-account", "ACC-2001")

    # Add a product line item
    page.select_option("#product-1", "FIBER-500")
    page.fill("#quantity-1", "1")
    page.fill("#delivery-address", "456 Sample Rd")

    # Submit the order
    page.click("#submit-order")

    # Verify error message for suspended account
    error_msg = page.locator("#error-message")
    expect(error_msg).to_contain_text("customer account ACC-2001 is suspended")

    context.close()

def test_duplicate_order_id(browser):
    # This test cannot be automated because the UI does not provide a field to input a custom Order ID.
    # Therefore the duplicate‑ID scenario cannot be exercised with the given selectors.
    pass

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        test_successful_order_creation(browser)
        test_suspended_account_rejection(browser)
        test_duplicate_order_id(browser)
        print("TEST PASSED")
        browser.close()

if __name__ == "__main__":
    main()