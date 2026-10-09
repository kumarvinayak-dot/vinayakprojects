from playwright.sync_api import sync_playwright, expect

def test_successful_order_creation(page):
    # Step 1 – Log in as CSR (using demo credentials of the site)
    page.goto("https://the-internet.herokuapp.com/login")
    page.fill('#username', 'tomsmith')
    page.fill('#password', 'SuperSecretPassword!')
    page.click('button[type="submit"]')
    # Verify login succeeded
    expect(page.locator('#flash')).to_contain_text('You logged into a secure area!')
    # Steps 2‑8 cannot be automated because the demo site does not contain CRM or order‑creation UI.
    # Skipping: verify active customer, open "Create New Order", fill order details, submit, and validate confirmation.

def test_rejection_for_suspended_account(page):
    # Step 1 – Log in as CSR
    page.goto("https://the-internet.herokuapp.com/login")
    page.fill('#username', 'tomsmith')
    page.fill('#password', 'SuperSecretPassword!')
    page.click('button[type="submit"]')
    expect(page.locator('#flash')).to_contain_text('You logged into a secure area!')
    # Steps 2‑7 cannot be automated because there is no concept of suspended accounts or order workflow on this site.
    # Skipping: locate suspended customer, attempt order creation, and verify rejection message.

def test_order_creation_no_product_line_items(page):
    # Step 1 – Log in as CSR
    page.goto("https://the-internet.herokuapp.com/login")
    page.fill('#username', 'tomsmith')
    page.fill('#password', 'SuperSecretPassword!')
    page.click('button[type="submit"]')
    expect(page.locator('#flash')).to_contain_text('You logged into a secure area!')
    # Steps 2‑7 cannot be automated because the site lacks order creation forms and validation for product line items.
    # Skipping: verify active customer, open order form, submit without products, and check error handling.

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=800)
    context = browser.new_context()
    page = context.new_page()

    test_successful_order_creation(page)
    test_rejection_for_suspended_account(page)
    test_order_creation_no_product_line_items(page)

    print('TEST PASSED')

    context.close()
    browser.close()