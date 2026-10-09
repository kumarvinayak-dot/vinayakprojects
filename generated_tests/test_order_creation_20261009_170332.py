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
    # wait for navigation to orders page
    expect(page).to_have_url("http://127.0.0.1:5050/orders")
    # verify logged in user display
    if username == CSR_USERNAME:
        expect(page.locator("#current-user")).to_have_text("Asha (CSR)")
    else:
        expect(page.locator("#current-user")).to_have_text("Ravi (Supervisor)")

def logout(page):
    page.click("#logout-link")
    expect(page).to_have_url(START_URL)

def test_successful_order_creation(page):
    # 1. Log in as CSR
    login(page, CSR_USERNAME, CSR_PASSWORD)

    # 2. Verify selected customer account is Active (we will select an active one)
    page.click("#nav-create-order")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")
    page.select_option("#customer-account", "ACC-1001")  # Active account
    expect(page.locator("#customer-account")).to_have_value("ACC-1001")

    # 4. Enter unique Order ID
    page.fill("#order-id-input", "ORD-10001")

    # 5. Customer account reference already selected (ACC-1001)

    # 6. Add at least one product line item
    page.select_option("#product-1", "FIBER-100")
    page.fill("#quantity-1", "2")

    # 7. Submit the order
    page.click("#submit-order")

    # Expected success message
    expect(page.locator("#success-message")).to_have_text(
        "Order ORD-10001 created successfully with status New."
    )

    # Verify order detail page fields
    expect(page.locator("#order-id")).to_have_text("ORD-10001")
    expect(page.locator("#order-status")).to_have_text("New")
    expect(page.locator("#order-customer")).to_contain_text("ACC-1001")
    # Charges default to $0.00
    expect(page.locator("#order-charges")).to_have_text("$0.00")

    # Clean up: logout for next test
    logout(page)

def test_order_creation_suspended_account(page):
    # 1. Log in as CSR
    login(page, CSR_USERNAME, CSR_PASSWORD)

    # 2. Select a suspended customer account
    page.click("#nav-create-order")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")
    page.select_option("#customer-account", "ACC-2001")  # Suspended account
    expect(page.locator("#customer-account")).to_have_value("ACC-2001")

    # 4. Fill Order ID and product line
    page.fill("#order-id-input", "ORD-10002")
    page.select_option("#product-1", "FIBER-500")
    page.fill("#quantity-1", "1")

    # 5. Submit the order
    page.click("#submit-order")

    # Expected error message for suspended account
    expect(page.locator("#error-message")).to_have_text(
        "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
    )

    # Ensure no order detail page is shown (still on create page)
    expect(page).not_to_have_url(lambda url: "orders/" in url and not url.endswith("/new"))

    logout(page)

def test_duplicate_order_id(page):
    # 1. Log in as CSR
    login(page, CSR_USERNAME, CSR_PASSWORD)

    # Create an initial order with ID ORD-10003
    page.click("#nav-create-order")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")
    page.select_option("#customer-account", "ACC-1002")  # Active account
    page.fill("#order-id-input", "ORD-10003")
    page.select_option("#product-1", "TV-BASIC")
    page.fill("#quantity-1", "1")
    page.click("#submit-order")
    expect(page.locator("#success-message")).to_have_text(
        "Order ORD-10003 created successfully with status New."
    )
    # Return to orders list to start duplicate attempt
    page.goto("http://127.0.0.1:5050/orders")
    expect(page).to_have_url("http://127.0.0.1:5050/orders")

    # 3. Initiate “Create New Order” again
    page.click("#nav-create-order")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")

    # 4. Enter the same Order ID
    page.select_option("#customer-account", "ACC-1001")  # Any active account
    page.fill("#order-id-input", "ORD-10003")
    page.select_option("#product-1", "ROUTER")
    page.fill("#quantity-1", "1")

    # 7. Submit the order
    page.click("#submit-order")

    # Expected duplicate ID error
    expect(page.locator("#error-message")).to_have_text(
        "Order ID ORD-10003 already exists. Please enter a unique Order ID."
    )

    logout(page)

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        test_successful_order_creation(page)
        test_order_creation_suspended_account(page)
        test_duplicate_order_id(page)

        print("TEST PASSED")

        context.close()
        browser.close()

if __name__ == "__main__":
    main()