"""Re-render docs/architecture.png from docs/architecture.html.

pip install playwright && playwright install chromium
python docs/render_diagram.py
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 860}, device_scale_factor=2)
    page.goto((HERE / "architecture.html").as_uri())
    page.wait_for_timeout(300)
    page.locator("svg").screenshot(path=str(HERE / "architecture.png"))
    browser.close()
print("Wrote docs/architecture.png")
