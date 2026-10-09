from playwright.sync_api import sync_playwright, expect

START_URL = "http://127.0.0.1:5050/login"
CSR_USER = {"username": "csr1", "password": "Password123!"}
SUP_USER = {"username": "sup1", "password": "Super123!"}


def login(page, user):
    page.goto(START_URL)
    page.fill("#username", user["username"])
    page.fill("#password", user["password"])
    page.click("#login-button")
    # verify login success
    expect(page.locator("#current-user")).to_have_text(f"{'Asha' if user['username']=='csr1' else 'Ravi'} (CSR)" if user["username"] == "csr1" else f"Ravi (Supervisor)")


def logout(page):
    page.click("#logout-link")
    expect(page).to_have_url(START_URL)


def test_cancel_new_order(page):
    # Log in as customer (CSR)
    login(page, CSR_USER)

    # Open create order form
    page.click("#nav-create-order")
    expect(page).to_have_url("http://127.0.0.1:5050/orders/new")

    # Fill order details (auto ID)
    page.select_option("#customer-account", "ACC-1001")
    page.select_option("#product-1", "FIBER-100")
    page.fill("#delivery-address", "123 Test St")
    page.click("#submit-order")

    # Capture generated order ID
    order_id = page.locator("#order-id").inner_text().strip()
    expect(page.locator("#success-message")).to_contain_text(f"Order {order_id} created successfully with status New.")
    expect(page.locator("#order-status")).to_have_text("New")

    # Cancel the order
    page.click("#cancel-order-button")
    # No explicit confirmation dialog defined; assume immediate action
    expect(page.locator("#success-message")).to_contain_text(f"Order {order_id} has been cancelled")
    expect(page.locator("#order-status")).to_have_text("Cancelled")
    expect(page.locator("#order-charges")).to_have_text("$0.00")
    # Order should no longer be editable
    expect(page.locator("#edit-order-link")).to_be_hidden()


def test_cancel_provisioning_order_with_supervisor(page):
    # Log in as customer (CSR)
    login(page, CSR_USER)

    # Open seeded provisioning order
    order_id = "ORD-0003"
    page.goto(f"http://127.0.0.1:5050/orders/{order_id}")
    expect(page.locator("#order-status")).to_have_text("Provisioning")

    # Attempt cancellation
    page.click("#cancel-order-button")
    expect(page.locator("#info-message")).to_contain_text("Cancellation requires supervisor approval")

    # Log out and log in as supervisor
    logout(page)
    login(page, SUP_USER)

    # Supervisor opens the same order
    page.goto(f"http://127.0.0.1:5050/orders/{order_id}")
    expect(page.locator("#approval-status")).to_have_text("Cancellation pending supervisor approval.")

    # Approve cancellation
    page.click("#approve-cancel-button")
    expect(page.locator("#success-message")).to_contain_text("Cancellation of order")
    expect(page.locator("#order-status")).to_have_text("Cancelled")
    expect(page.locator("#order-charges")).to_have_text("$0.00")


def test_cancel_edge_cases(page):
    # Scenario A – Already Cancelled
    login(page, CSR_USER)
    cancelled_id = "ORD-0005"
    page.goto(f"http://127.0.0.1:5050/orders/{cancelled_id}")
    expect(page.locator("#order-status")).to_have_text("Cancelled")
    page.click("#cancel-order-button")
    # Expected message for already cancelled order
    expect(page.locator("#error-message")).to_contain_text("already cancelled")
    expect(page.locator("#order-status")).to_have_text("Cancelled")
    logout(page)

    # Scenario B – Completed
    login(page, CSR_USER)
    completed_id = "ORD-0004"
    page.goto(f"http://127.0.0.1:5050/orders/{completed_id}")
    expect(page.locator("#order-status")).to_have_text("Completed")
    page.click("#cancel-order-button")
    # Expected error for completed orders
    expect(page.locator("#error-message")).to_contain_text("cannot be cancelled")
    expect(page.locator("#order-status")).to_have_text("Completed")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=800)
        context = browser.new_context()
        page = context.new_page()
        try:
            test_cancel_new_order(page)
            page.context.clear_cookies()
            page.reload()
            test_cancel_provisioning_order_with_supervisor(page)
            page.context.clear_cookies()
            page.reload()
            test_cancel_edge_cases(page)
        finally:
            context.close()
            browser.close()
        print("TEST PASSED")


if __name__ == "__main__":
    main()