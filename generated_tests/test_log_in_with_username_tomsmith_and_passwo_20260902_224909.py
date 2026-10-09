from playwright.sync_api import sync_playwright, expect

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=1200)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://the-internet.herokuapp.com/login")
        page.fill("input#username", "tomsmith")
        page.fill("input#password", "SuperSecretPassword!")
        page.click('button[type="submit"]')
        success_msg = page.locator("div.flash.success")
        expect(success_msg).to_be_visible()
        expect(success_msg).to_contain_text("You logged into a secure area!")
        page.wait_for_timeout(2500)
        print("TEST PASSED")
        context.close()
        browser.close()

if __name__ == "__main__":
    run()