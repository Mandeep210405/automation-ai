"""
recorder.py — "WRITE STEP INTO TESTCASE" box of the diagram.
Keeps every passed step and every attempt, then writes:
  * testcase.xlsx   (Test Case, Execution Log, Test Data, Run Stats sheets)
  * test_script.py  (stand-alone Playwright + Firefox script of the working steps)
  * testcase.json   (machine-readable copy)
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .prompts import placeholders

def _ms(seconds_text) -> int:
    try:
        return int(float(seconds_text or 1) * 1000)
    except ValueError:
        return 1000


def _doc(text: str) -> str:
    return (text or "").replace(chr(92), "/").replace(chr(34) * 3, chr(39) * 3)


SECRET_KEY = re.compile(r"pass|secret|token|key|pin|otp|pwd", re.I)

ACTION_LABEL = {
    "click": "Click", "fill": "Enter text", "select_option": "Select option", "check": "Check",
    "uncheck": "Uncheck", "press": "Press key", "hover": "Hover", "navigate": "Navigate", "wait": "Wait",
    "assert_visible": "Verify visible", "assert_not_visible": "Verify not visible",
    "assert_text_visible": "Verify text visible", "assert_url_contains": "Verify URL contains",
    "assert_title_contains": "Verify title contains", "assert_value": "Verify field value",
    "assert_enabled": "Verify enabled", "assert_disabled": "Verify disabled", "skip": "Skipped",
    "manual": "Manual (human)",
}


def describe_element(el: dict | None) -> str:
    if not el:
        return ""
    text = f'{el.get("role") or el["tag"]} "{el.get("name") or el.get("placeholder") or ""}"'
    for key, label in (("dialog", "in dialog"), ("form", "in form"), ("row", "in row")):
        if el.get(key):
            text += f' {label} "{el[key][:60]}"'
            break
    return text


class Recorder:
    def __init__(self, run_dir, url, request, login_required, session_file, test_data_file, cfg):
        self.run_dir = run_dir
        self.url = url
        self.request = request
        self.login_required = login_required
        self.session_file = session_file
        self.test_data_file = test_data_file
        self.cfg = cfg
        self.tc_id = "TC_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        self.title = ""
        self.objective = ""
        self.preconditions: list[str] = []
        self.steps: list[dict] = []
        self.log: list[dict] = []
        self.captures: list[dict] = []
        self.status = "In progress"

    # ---------------------------------------------------------------- record
    def set_plan(self, plan: dict):
        self.title = plan.get("title", "")
        self.objective = plan.get("objective", "")
        self.preconditions = list(plan.get("preconditions") or [])
        if self.login_required:
            self.preconditions.insert(0, "User is logged in (saved session is loaded).")

    def add_step(self, plan_index: int, description: str, expected: str, result: dict, attempts: int,
                 screenshot: str | None, status: str = "Passed", note: str = ""):
        self.steps.append({
            "plan_index": plan_index, "description": description, "expected": expected,
            "action": result.get("action"), "target": describe_element(result.get("element")),
            "locator": result.get("locator_code") or "", "locator_quality": result.get("locator_quality") or "",
            "value_template": result.get("value_template") or "", "new_page": result.get("new_page", False),
            "xpath": result.get("xpath") or "", "xpath_quality": result.get("xpath_quality") or "", 
            "frame_note": result.get("frame_note", False),
            "status": status, "attempts": attempts, "screenshot": screenshot or "",
            "actual": note or ("As expected" if status == "Passed" else status),
        })

    def add_log(self, plan_index: int, attempt: int, source: str, action: dict, ok: bool, error: str = ""):
        self.log.append({
            "time": datetime.now().strftime("%H:%M:%S"), "step": plan_index + 1, "attempt": attempt,
            "source": source, "action": action.get("action", ""), "target": action.get("target", ""),
            "value": action.get("value", ""), "result": "PASS" if ok else "FAIL", "error": error,
            "reason": action.get("reason", ""),
        })

    def _numbered_steps(self):
        per_plan = defaultdict(list)
        for s in self.steps:
            per_plan[s["plan_index"]].append(s)
        out = []
        for s in self.steps:
            group = per_plan[s["plan_index"]]
            n = s["plan_index"] + 1
            s["no"] = str(n) if len(group) == 1 else f"{n}.{group.index(s) + 1}"
            out.append(s)
        return out

    # ---------------------------------------------------------------- outputs
    def save_all(self, usage: dict, test_data: dict):
        steps = self._numbered_steps()
        xlsx = self.run_dir / "testcase.xlsx"
        script = self.run_dir / "test_script.py"
        js = self.run_dir / "testcase.json"
        self._save_excel(xlsx, steps, usage, test_data)
        script.write_text(self._script(steps), encoding="utf-8")
        js.write_text(json.dumps({
            "id": self.tc_id, "title": self.title, "objective": self.objective, "url": self.url,
            "request": self.request, "preconditions": self.preconditions, "status": self.status,
            "login_required": self.login_required,
            "steps": steps, "log": self.log, "captures": self.captures, "llm_usage": usage,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        return xlsx, script, js

    def _save_excel(self, path, steps, usage, test_data):
        wb = Workbook()
        thin = Side(style="thin", color="BFBFBF")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        head_fill = PatternFill("solid", fgColor="1F4E78")
        head_font = Font(bold=True, color="FFFFFF")
        wrap = Alignment(wrap_text=True, vertical="top")
        fills = {"Passed": "C6EFCE", "Failed": "FFC7CE", "Skipped": "FFEB9C", "Manual": "FFEB9C"}

        def table(ws, start_row, headers, rows, widths):
            for c, h in enumerate(headers, 1):
                cell = ws.cell(row=start_row, column=c, value=h)
                cell.fill, cell.font, cell.border, cell.alignment = head_fill, head_font, border, wrap
            for r, row in enumerate(rows, start_row + 1):
                for c, v in enumerate(row, 1):
                    cell = ws.cell(row=r, column=c, value=v)
                    cell.border, cell.alignment = border, wrap
            for c, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(c)].width = w

        # --- Sheet 1: Test Case
        ws = wb.active
        ws.title = "Test Case"
        info = [
            ("Test Case ID", self.tc_id), ("Title", self.title), ("Objective", self.objective),
            ("Tester Request", self.request), ("Start URL", self.url),
            ("Preconditions", "\n".join(f"- {p}" for p in self.preconditions)),
            ("Login Required", "Yes" if self.login_required else "No"),
            ("Browser", "Firefox (Playwright)"), ("Models", ", ".join(f"{k}={v}" for k, v in self.cfg.MODELS.items())),
            ("Generated On", datetime.now().strftime("%Y-%m-%d %H:%M")), ("Overall Status", self.status),
        ]
        for r, (k, v) in enumerate(info, 1):
            ws.cell(row=r, column=1, value=k).font = Font(bold=True)
            ws.cell(row=r, column=2, value=v).alignment = wrap
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=8)
        status_cell = ws.cell(row=len(info), column=2)
        status_cell.font = Font(bold=True)
        status_cell.fill = PatternFill("solid", fgColor="C6EFCE" if self.status == "Passed" else "FFEB9C")

        start = len(info) + 2
        headers = ["Step No", "Step Description", "Action", "Target Element", "Test Data", "Expected Result",
                   "Actual Result", "Status", "Locator (Playwright Python)", "Locator Quality", "Attempts", "Screenshot"]
        rows = [[s["no"], s["description"], ACTION_LABEL.get(s["action"], s["action"]), s["target"],
                 s["value_template"], s["expected"], s["actual"], s["status"], s["locator"], s["locator_quality"],
                 s["attempts"], s["screenshot"]] for s in steps]
        table(ws, start, headers, rows, [8, 42, 16, 32, 22, 36, 22, 10, 55, 12, 9, 30])
        for r, s in enumerate(steps, start + 1):
            color = fills.get(s["status"])
            if color:
                ws.cell(row=r, column=8).fill = PatternFill("solid", fgColor=color)
        ws.freeze_panes = ws.cell(row=start + 1, column=3)
        ws.column_dimensions["A"].width = 16

        # --- Sheet 2: Execution Log
        ws2 = wb.create_sheet("Execution Log")
        table(ws2, 1, ["Time", "Plan Step", "Attempt", "Source", "Action", "Target", "Value", "Result", "Error", "AI Reason"],
              [[l["time"], l["step"], l["attempt"], l["source"], l["action"], l["target"], l["value"], l["result"],
                l["error"], l["reason"]] for l in self.log],
              [10, 9, 8, 10, 16, 8, 24, 8, 60, 50])
        ws2.freeze_panes = "A2"

        # --- Sheet 3: Test Data (secrets masked)
        ws3 = wb.create_sheet("Test Data")
        table(ws3, 1, ["Key", "Value", "Used As"],
              [[k, "******" if SECRET_KEY.search(k) else str(v), "{{" + k + "}}"] for k, v in test_data.items()],
              [22, 40, 22])

        # --- Sheet 4: Run Stats (filtration evaluation)
        ws4 = wb.create_sheet("Run Stats")
        cap_rows = [[c["capture_id"], c["purpose"], c["url"], c["dom_elements"], c["raw_html_chars"],
                     c["approx_raw_tokens"], c["interactive_kept"], c["elements_shown"], c["llm_chars"],
                     c["approx_llm_tokens"], c["reduction"], c["capture_ms"]] for c in self.captures]
        table(ws4, 1, ["Capture", "Purpose", "URL", "DOM Elements", "Raw HTML chars", "~Raw tokens",
                       "Interactive kept", "Elements sent", "LLM context chars", "~LLM tokens", "Reduction", "Capture ms"],
              cap_rows, [9, 12, 45, 13, 15, 12, 15, 13, 17, 12, 11, 11])
        r0 = len(cap_rows) + 3
        table(ws4, r0, ["LLM Role", "Model", "Calls", "Prompt tokens", "Output tokens", "Seconds"],
              [[role, self.cfg.MODELS.get(role, ""), u["calls"], u["prompt_tokens"], u["output_tokens"],
                round(u["seconds"], 1)] for role, u in usage.items()],
              [9, 12, 45, 13, 15, 12])
        wb.save(path)

    def _script(self, steps) -> str:
        storage = str(self.session_file.resolve()) if self.login_required and self.session_file else None
        L = [
            f'"""Auto-generated by AI Test Case Generator.',
            f"{self.tc_id}: {_doc(self.title)}",
            f"Request: {_doc(self.request)}",
            "Run:  python test_script.py",
            '"""',
            "import json",
            "import os",
            "import re",
            "from pathlib import Path",
            "",
            "from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout, expect, sync_playwright",
            "",
            f"START_URL = {json.dumps(self.url)}",
            f"STORAGE_STATE = {json.dumps(storage)}  # saved login session (None = no login)",
            f"TEST_DATA_FILE = {json.dumps(str(self.test_data_file.resolve()))}",
            "HEADLESS = False",
            "",
            "",
            "def load_data():",
            "    p = Path(TEST_DATA_FILE)",
            "    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}",
            "",
            "",
            "DATA = load_data()",
            "",
            "",
            "def fmt(template):",
            "    \"\"\"Replace {{key}} with test data. Env var TCG_<KEY> overrides the file.\"\"\"",
            "    def repl(m):",
            "        key = m.group(1)",
            "        return os.environ.get('TCG_' + key.upper(), str(DATA.get(key, '')))",
            "    return re.sub(r'\\{\\{\\s*([\\w.-]+)\\s*\\}\\}', repl, template)",
            "",
            "",
            "def settle(page):",
            "    for state, t in (('domcontentloaded', 5000), ('networkidle', 4000)):",
            "        try:",
            "            page.wait_for_load_state(state, timeout=t)",
            "        except (PWTimeout, PWError):",
            "            pass",
            "    page.wait_for_timeout(400)",
            "",
            "",
            "def select(locator, text):",
            "    try:",
            "        locator.select_option(label=text)",
            "    except (PWTimeout, PWError):",
            "        locator.select_option(value=text)",
            "",
            "",
            "def run():",
            "    with sync_playwright() as p:",
            "        browser = p.firefox.launch(headless=HEADLESS)",
            "        use_state = STORAGE_STATE if STORAGE_STATE and Path(STORAGE_STATE).exists() else None",
            f"        context = browser.new_context(storage_state=use_state, viewport={json.dumps(self.cfg.VIEWPORT)})",
            f"        context.set_default_timeout({self.cfg.ACTION_TIMEOUT_MS})",
            f"        expect.set_options(timeout={self.cfg.ASSERT_TIMEOUT_MS})",
            "        page = context.new_page()",
            "        page.goto(START_URL, wait_until='domcontentloaded')",
            "        settle(page)",
        ]
        for s in steps:
            L.append("")
            L.append(f"        # Step {s['no']}: {s['description']}".replace("\n", " "))
            L.append(f"        # Expected: {s['expected']}".replace("\n", " "))
            if s["locator_quality"] == "unverified":
                L.append("        # WARNING: no unique stable locator was found; CSS path below may be fragile.")
            L += ["        " + line for line in self._step_code(s)]
        L += [
            "",
            "        print('TEST PASSED')",
            "        context.close()",
            "        browser.close()",
            "",
            "",
            "if __name__ == '__main__':",
            "    run()",
            "",
        ]
        return "\n".join(L)

    @staticmethod
    def _step_code(s) -> list[str]:
        a, loc, tpl = s["action"], s["locator"], s["value_template"]
        if s["status"] in ("Skipped",) or a == "skip":
            return ["pass  # skipped during generation"]
        if a == "manual":
            return ["# MANUAL STEP: performed by a human during generation; automate it here.",
                    "input('Perform this step manually, then press Enter...')"]
        val = f"fmt({json.dumps(tpl, ensure_ascii=False)})" if placeholders(tpl) else json.dumps(tpl, ensure_ascii=False)
        code = {
            "click": f"{loc}.click()",
            "fill": f"{loc}.fill({val})",
            "select_option": f"select({loc}, {val})",
            "check": f"{loc}.check()",
            "uncheck": f"{loc}.uncheck()",
            "hover": f"{loc}.hover()",
            "press": f"{loc}.press({val})" if loc else f"page.keyboard.press({val})",
            "navigate": f"page.goto({val}, wait_until='domcontentloaded')",
            "wait": f"page.wait_for_timeout({_ms(tpl)})",
            "assert_visible": f"expect({loc}).to_be_visible()",
            "assert_not_visible": f"expect({loc}).to_be_hidden()",
            "assert_text_visible": f"expect(page.get_by_text({val}).first).to_be_visible()",
            "assert_url_contains": f"expect(page).to_have_url(re.compile(re.escape({val})))",
            "assert_title_contains": f"expect(page).to_have_title(re.compile(re.escape({val})))",
            "assert_value": f"expect({loc}).to_have_value({val})",
            "assert_enabled": f"expect({loc}).to_be_enabled()",
            "assert_disabled": f"expect({loc}).to_be_disabled()",
        }.get(a, f"pass  # unsupported action {a}")
        if a == "click" and s.get("new_page"):
            return ["with context.expect_page() as new_page_info:", f"    {code}",
                    "page = new_page_info.value", "settle(page)"]
        if not a.startswith("assert") and a != "wait":
            return [code, "settle(page)"]
        return [code]
