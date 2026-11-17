# AI Test Case Generator (Playwright + Firefox + Ollama)

You give a **URL** and a **plain-English test request**. The tool opens Firefox, handles login like a real
browser (saved session), filters the DOM, lets a **local, free** Ollama model plan and perform the test step by
step, verifies every step by actually executing it, and gives you:

- `testcase.xlsx` — test case with steps, test data, expected results, status, verified locators, screenshots, execution log, run stats
- `test_script.py` — a stand-alone Playwright + Firefox script of the working steps
- `testcase.json`, screenshots, and every filtered snapshot (for R&D review)

## 1. Setup (Windows, one time)

```
cd DOM-Filtration-RnD\testcase-generator
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install firefox
```

Install Ollama from https://ollama.com/download (free), start it, then pull the model:

```
ollama pull qwen2.5:7b
```

Check everything:

```
python tools/check_setup.py
```

## 2. Run

```
python main.py
```

It asks for the URL, the test you want, and whether login is required. Or pass everything:

```
python main.py --url https://example.com --prompt "Verify the More information link opens the IANA page" --login no
```

**Login required = yes:** Firefox opens. If a saved session exists you can reuse it. If you are not logged in,
log in yourself in the window (OTP, SSO and captcha all work), then press ENTER in the terminal. The session
(cookies, localStorage, IndexedDB) is saved in `sessions/<site>.json` and reused next time and by the generated script.

**Test data:** put values in `test_data.json` (e.g. `email`, `password`). The AI only sees the key names and
writes `{{email}}`; real values are filled in at execution time and are never sent to the model. Secrets are
masked in the Excel file.

Output goes to `runs/<date>_<request>/`.

## 3. Which model does what (all free, local)

| Role | Default | What it does (diagram box) |
|---|---|---|
| planner | `qwen2.5:7b` | ANALYSE page + DEFINE FLOW OF STEPS; updates the flow when the page changes |
| step | `qwen2.5:7b` | AI → PATH OF TEST STEP: one concrete action per call |
| repair | `qwen2.5:7b` | AI ANALYSE: error + failed path + NEW DOM → corrected action |

One model for all roles keeps it loaded in memory (switching models is slow). Change models in `config.py`:
`qwen2.5:3b` for weak PCs, `qwen2.5:14b` for a 12–16 GB GPU, or `qwen3:8b` with `OLLAMA_THINK = False`.

## 4. How it maps to your flowchart

```
START -> DOM + PROMPT -> FILTER PROCESS (extractor.js + dom_filter.py)
      -> AI planner: ANALYSE -> DEFINE FLOW OF STEPS
      -> for each step: AI step -> PATH OF TEST STEP -> EXECUTOR ENGINE -> PASS/FAIL
           PASS -> WRITE STEP INTO TESTCASE -> UPDATED FDOM (re-capture) -> planner updates flow if page changed
           FAIL -> FAIL ERROR + PATH + NEW DOM -> AI repair -> new path -> executor
                -> after RETRY_LIMIT: HUMAN INTERVENTION (do it manually / retry / skip / abort)
      -> Excel + script
```

| File | Box |
|---|---|
| `tcgen/extractor.js` | Filter process, inside the browser (Layer 1 controls + Layer 2 context) |
| `tcgen/dom_filter.py` | Merge frames, rank by relevance, fit token budget, render for the LLM |
| `tcgen/prompts.py` | Prompts, JSON schemas, fixed action list, validation of AI output |
| `tcgen/llm.py` | Ollama client with structured JSON output and token accounting |
| `tcgen/executor.py` | Executor engine, locator generation + validation |
| `tcgen/agent.py` | The loop above, retries, human intervention, re-planning |
| `tcgen/recorder.py` | Excel, Playwright script, JSON |
| `tcgen/browser.py` | Firefox launch, login, session save/reuse |

## 5. Why it stays fast and cheap on huge DOMs (e.g. 300k lines)

1. **Filtering runs inside Firefox in one pass.** Python never downloads or parses raw HTML. `script`, `style`,
   `svg` and every `display:none` subtree are skipped as a whole.
2. **Two layers.** Layer 1 finds controls by many signals (native tags, ARIA roles, tabindex, handlers,
   contenteditable, own `cursor:pointer`), so custom React/MUI controls are kept. Layer 2 attaches meaning to each
   control: label, hint, form, section, dialog, table row and column. Page context (headings, alerts, validation
   messages, dialogs, tables, breadcrumbs) is collected once.
3. **Fixed token budget per AI call.** Before each call, elements are ranked by relevance to the current step.
   Open dialogs, the focused field and whole relevant forms are always kept. The rest fills the budget
   (`STEP_CONTEXT_TOKENS`, default 3000). In a test with 20,000 table buttons, ranking + rendering took ~0.7 s,
   produced ~3,000 tokens, and still included the exact row asked for.
4. **Verified locators.** Every kept element is tagged during capture. A locator candidate (test id, id, role+name,
   label, placeholder, name, CSS path) is accepted only if it matches exactly one element and that element is the
   one the AI chose. Auto-generated IDs (`:r5:`, `mui-123`) are never used. The step is then executed with that
   locator, so a passed step proves its locator works.

## 6. R&D: test the filtration alone (no AI)

```
python tools/filter_only.py --url https://example.com
python tools/filter_only.py --url https://your-app --login --query "create a new batch"
```

It saves `raw_dom.html`, `page.png`, `snapshot.json` and `llm_context.txt` (exactly what the model would see) and
prints DOM size, kept controls, tokens, reduction, and a breakdown by signal. Elements kept only because of
`pointer` or `tabindex` are where false positives come from.

Each normal run also saves every capture under `runs/.../snapshots/` and an evaluation table in the
**Run Stats** sheet.

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot reach Ollama` | Start the Ollama app; check `http://localhost:11434` opens in a browser |
| `Model ... is not pulled` | `ollama pull qwen2.5:7b` |
| `Executable doesn't exist` (Firefox) | `python -m playwright install firefox` |
| PowerShell blocks `activate` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Very slow AI calls | No GPU: use `qwen2.5:3b`; first call is slow while the model loads |
| Warning "prompt filled the whole context window" | Raise `OLLAMA_NUM_CTX` or lower the `*_CONTEXT_TOKENS` budgets |
| Session expired | Answer "n" to "Reuse saved session?" and log in again |

## 8. Known limits (honest list)

- Closed shadow DOM, canvas-drawn UIs and cross-origin iframes the browser blocks cannot be read.
- Drag-and-drop, file upload dialogs and CAPTCHA inside the test are not automated (use human intervention).
- Steps are generated by a 7B model: review the Excel before using it as a formal test case.
- Data created during generation (e.g. a new record) really is created on the site. Use a test environment.
