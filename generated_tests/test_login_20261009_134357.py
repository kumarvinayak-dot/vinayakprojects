from playwright.sync_api import sync_playwright, expect

START_URL = "https://the-internet.herokuapp.com/login"

def test_valid_login(page):
    # Open login page
    page.goto(START_URL)
    # Enter valid credentials
    page.fill("#username", "tomsmith")
    page.fill("#password", "SuperSecretPassword!")
    # Click Login
    page.click("button[type=\"submit\"]")
    # Verify successful login message
    expect(page.locator("#flash")).to_contain_text("You logged into a secure area!")

def test_invalid_password(page):
    # Open login page
    page.goto(START_URL)
    # Enter username with invalid password
    page.fill("#username", "tomsmith")
    page.fill("#password", "wrongpass")
    # Click Login
    page.click("button[type=\"submit\"]")
    # Verify error message
    expect(page.locator("#flash")).to_contain_text("Your password is invalid!")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    
    test_valid_login(page)
    test_invalid_password(page)
    
    print("TEST PASSED")
    
    context.close()
    browser.close()