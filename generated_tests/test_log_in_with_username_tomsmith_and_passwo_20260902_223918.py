from playwright.sync_api import sync_playwright, expect

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://the-internet.herokuapp.com/login")
        page.fill('input#username', 'tomsmith')
        page.fill('input#password', 'SuperSecretPassword!')
        page.click('button[type="submit"]')
        expect(page.locator('#flash')).to_contain_text('You logged into a secure area!')
        print('TEST PASSED')
        context.close()
        browser.close()

if __name__ == "__main__":
    run()