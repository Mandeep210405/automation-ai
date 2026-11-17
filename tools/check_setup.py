"""Check everything is installed:  python tools/check_setup.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

ok = True
print(f"Python {sys.version.split()[0]}", "OK" if sys.version_info >= (3, 9) else "-> need 3.9+")
for mod in ("playwright", "requests", "openpyxl"):
    try:
        __import__(mod)
        print(f"package {mod}: OK")
    except ImportError:
        ok = False
        print(f"package {mod}: MISSING -> pip install -r requirements.txt")

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.firefox.launch(headless=True)
        b.new_page().goto("about:blank")
        b.close()
    print("Playwright Firefox: OK")
except Exception as exc:
    ok = False
    print(f"Playwright Firefox: FAILED -> python -m playwright install firefox\n   {str(exc)[:200]}")

from tcgen.llm import OllamaClient  # noqa: E402
problems = OllamaClient(config).check()
if problems:
    ok = False
    for pr in problems:
        print("Ollama:", pr)
else:
    print(f"Ollama + models {sorted(set(config.MODELS.values()))}: OK")

print("\nALL GOOD - run: python main.py" if ok else "\nFix the items above, then run this check again.")
