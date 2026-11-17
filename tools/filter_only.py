"""
R&D tool: run ONLY the DOM capture + filtration on a page (no AI).

    python tools/filter_only.py --url https://example.com
    python tools/filter_only.py --url https://site --login --query "create a new batch"

Saves raw DOM, screenshot, full snapshot JSON and the exact text the LLM would
receive, and prints the numbers you need for evaluation (recall, noise, tokens).
"""
import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from tcgen.browser import open_session  # noqa: E402
from tcgen.dom_filter import capture, render_for_llm  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True)
ap.add_argument("--login", action="store_true", help="log in first (session is saved/reused)")
ap.add_argument("--query", default="", help="pretend step text used for relevance ranking")
ap.add_argument("--budget", type=int, default=config.STEP_CONTEXT_TOKENS)
args = ap.parse_args()

out = Path(config.RUNS_DIR) / f"filter_{datetime.now():%Y%m%d_%H%M%S}"
out.mkdir(parents=True, exist_ok=True)

with sync_playwright() as pw:
    browser, context, page, _ = open_session(pw, config, args.url, args.login)
    input("Navigate/open whatever state you want to capture in Firefox, then press ENTER... ")
    raw = page.content()
    (out / "raw_dom.html").write_text(raw, encoding="utf-8")
    page.screenshot(path=str(out / "page.png"), full_page=True)
    t0 = time.time()
    snap = capture(page, 1, config)
    ms = int((time.time() - t0) * 1000)
    text, st = render_for_llm(snap, args.query, args.budget)
    (out / "snapshot.json").write_text(json.dumps(snap.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "llm_context.txt").write_text(text, encoding="utf-8")
    context.close()
    browser.close()

sig = Counter("+".join(e["signals"]) for e in snap.elements)
roles = Counter(e.get("role") or e["tag"] for e in snap.elements)
print(f"\nURL             : {snap.url}")
print(f"DOM elements    : {snap.stats.get('dom_elements', 0):,}   (hidden subtrees pruned: {snap.stats.get('pruned_hidden_subtrees', 0):,})")
print(f"Raw HTML        : {len(raw):,} chars  (~{len(raw) // 4:,} tokens)")
print(f"Interactive kept: {len(snap.elements)}   capture time: {ms} ms")
print(f"Sent to LLM     : {st['elements_shown']} elements, {st['chars']:,} chars (~{st['approx_tokens']:,} tokens)")
print(f"Reduction       : {(1 - st['chars'] / max(len(raw), 1)) * 100:.2f}%")
print(f"By role         : {dict(roles.most_common(12))}")
print(f"By signal       : {dict(sig.most_common(12))}   <- 'pointer'/'tabindex' only = check for false positives")
print(f"Context         : {len(snap.context['headings'])} headings, {len(snap.context['messages'])} messages, "
      f"{len(snap.context['dialogs'])} dialogs, {len(snap.context['tables'])} tables")
print(f"\nFiles in {out}: raw_dom.html, page.png, snapshot.json, llm_context.txt")
print("Compare page.png with llm_context.txt: is every control you can see listed (recall)? anything junk (noise)?")
