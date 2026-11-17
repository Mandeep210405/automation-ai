"""
master_export.py — writes a generated test case in the "Automation Master Controller" format:

    Sheet  App_Config           (only when no template is used)
    Sheet  All_Cases_List       (one row per test case: Id, name, Positive/Negative, flag, description)
    Sheet  <case>_Positive      (Test Step Id | Test Step | Locator Type | Locator Value | Test Data |
                                 Move To Test Step Id | Step Description)

Locators are XPath (captured and verified in the browser by executor.py).
If config.MASTER_TEMPLATE exists, it is COPIED and the new sheet + list row are added to the copy,
so your original file is never touched.
"""
from __future__ import annotations

import json
import re
import shutil
from copy import copy
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .prompts import placeholders

STEP_HEADERS = ["Test Step Id", "Test Step", "Locator Type", "Locator Value", "Test Data",
                "Move To Test Step Id", "Step Description"]
LIST_HEADERS = ["Test Case Id", "Test Cases", "Scenario Type", "Flag", "Description"]
CONFIG_HEADERS = ["Application_Type", "WEB URL OR MOBILE APP", "Application Name",
                  "Browser_Selection (Only Applicable to WEB App)", "Device_Type (Only Applicable to MOBILE App)",
                  "Platform (Only Applicable to MOBILE App)", "Device_Name (Only Applicable to MOBILE App)"]

# generator action -> (master keyword, needs locator)
DEFAULT_KEYWORDS = {
    "navigate": "navigateURL", "wait": "delay", "click": "click", "fill": "setText",
    "check": "click", "uncheck": "click", "select_option": "selectDropdown", "hover": "mouseHover",
    "press": "pressKey", "assert_visible": "verifyElementPresent", "assert_text_visible": "verifyElementPresent",
    "assert_not_visible": "verifyElementNotPresent", "assert_enabled": "verifyElementPresent",
    "assert_disabled": "verifyElementPresent", "assert_value": "verifyText",
    "assert_url_contains": "verifyURL", "assert_title_contains": "verifyTitle",
}


def xpath_literal(s: str) -> str:
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in s.split("'")) + ")"


def _fill(template: str, data: dict) -> str:
    for key in placeholders(template or ""):
        template = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", lambda _m: str(data.get(key, "")), template)
    return template


def _case_name(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (title or "generated_testcase").lower()).strip("_")
    return (s[:22].strip("_") or "generated_testcase")


def build_rows(steps: list[dict], test_data: dict, cfg) -> tuple[list[list], list[str]]:
    """Convert recorded steps to master rows. Returns (rows, warnings)."""
    kw = {**DEFAULT_KEYWORDS, **getattr(cfg, "MASTER_KEYWORDS", {})}
    delay_click = float(getattr(cfg, "MASTER_DELAY_AFTER_CLICK_S", 1))
    delay_nav = float(getattr(cfg, "MASTER_DELAY_AFTER_NAVIGATE_S", 2))
    warnings: list[str] = []
    out: list[list] = []          # [keyword, loc_type, loc_value, data, description]

    def add(keyword, ltype, lvalue, data, desc):
        out.append([keyword, ltype, lvalue, data, desc])

    out_first = True
    for s in steps:
        a = s.get("action")
        desc = (s.get("description") or "").strip()
        status = s.get("status")
        if status in ("Skipped", "Manual") or a in ("skip", "manual"):
            warnings.append(f"Step {s.get('no')} ({status}) was not exported: {desc}")
            continue
        keyword = kw.get(a)
        if not keyword:
            warnings.append(f"Step {s.get('no')}: no master keyword for action '{a}' - skipped.")
            continue
        value = _fill(s.get("value_template", ""), test_data)
        xp = (s.get("xpath") or "").strip()

        if a == "navigate":
            add(keyword, "NA", "NA", value or "NA", desc or "Navigate")
            add("delay", "NA", "NA", delay_nav, "")
            continue
        if a == "wait":
            try:
                sec = float(value)
            except ValueError:
                sec = 1
            add("delay", "NA", "NA", sec, desc)
            continue
        if a == "assert_text_visible":
            xp = f"(//*[contains(normalize-space(), {xpath_literal(value)})])[last()]"
            add(keyword, "XPATH", xp, "NA", desc)
            continue
        if a in ("assert_url_contains", "assert_title_contains"):
            add(keyword, "NA", "NA", value, desc)
            continue
        if not xp:
            warnings.append(f"Step {s.get('no')}: no XPath was captured - locator left EMPTY: {desc}")
        elif s.get("xpath_quality") == "positional":
            warnings.append(f"Step {s.get('no')}: XPath is positional (fragile): {xp}")
        if s.get("frame_note"):
            warnings.append(f"Step {s.get('no')}: element is inside an iframe - XPath is relative to that frame.")
        data = value if a in ("fill", "select_option", "press", "assert_value") else "NA"
        add(keyword, "XPATH", xp, data if data != "" else "NA", desc)
        if a in ("click", "check", "uncheck") and delay_click > 0:
            add("delay", "NA", "NA", delay_click, "")
    rows = []
    for n, (keyword, ltype, lvalue, data, desc) in enumerate(out, 1):
        rows.append([n, keyword, ltype, lvalue, data, "NA", desc or None])
    return rows, warnings


def _style_header(ws, headers, like=None):
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        if like is not None and like.cell(row=1, column=c).has_style:
            src = like.cell(row=1, column=c)
            cell.font, cell.fill, cell.alignment, cell.border = copy(src.font), copy(src.fill), copy(src.alignment), copy(src.border)
        else:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")


def export_master(tc: dict, test_data: dict, out_path, cfg, scenario: str = "Positive") -> tuple[Path, list[str]]:
    """tc = the testcase.json dict (title, url, request, login_required?, steps). Returns (path, warnings)."""
    out_path = Path(out_path)
    rows, warnings = build_rows(tc.get("steps", []), test_data, cfg)
    case = _case_name(tc.get("title"))
    template = Path(getattr(cfg, "MASTER_TEMPLATE", "") or "")
    using_template = template.is_file()

    if using_template:
        shutil.copyfile(template, out_path)
        wb = load_workbook(out_path)
    else:
        wb = Workbook()
        wb.remove(wb.active)
        ws = wb.create_sheet("App_Config")
        _style_header(ws, CONFIG_HEADERS)
        host = urlparse(tc.get("url", "")).netloc or "Application"
        for c, v in enumerate(["WEB", tc.get("url", ""), host.split(".")[0].upper(), "Chrome"], 1):
            ws.cell(row=2, column=c, value=v)
        dv = DataValidation(type="list", formula1='"Chrome,Firefox,Edge,All"'); ws.add_data_validation(dv); dv.add("D2:D50")
        ws = wb.create_sheet("All_Cases_List")
        _style_header(ws, LIST_HEADERS)
        ws.append([None, "Predefined_Suite_Execution", "NA", "N",
                   "- Work Flow Execution will Start with flagged Y\n- Please refer the Predefined_Suite_Execution Sheet for configuration"])

    # ---- All_Cases_List row
    lst = wb["All_Cases_List"]
    ids = [r[0].value for r in lst.iter_rows(min_row=2) if isinstance(r[0].value, (int, float))]
    next_id = int(max(ids)) + 1 if ids else 1
    row_no = max((r[0].row for r in lst.iter_rows(min_row=2) if any(c.value is not None for c in r)), default=1) + 1
    desc = "Login" if tc.get("login_required") else (tc.get("title") or "")
    for c, v in enumerate([next_id, case, scenario, "Y", desc], 1):
        lst.cell(row=row_no, column=c, value=v)
    dv = DataValidation(type="list", formula1='"Y,N"'); lst.add_data_validation(dv); dv.add(f"D{row_no}")
    dv2 = DataValidation(type="list", formula1='"Positive,Negative,All"'); lst.add_data_validation(dv2); dv2.add(f"C{row_no}")

    # ---- steps sheet  "<case>_Positive"
    sheet_name = f"{case}_{scenario}"[:31]
    base, k = sheet_name, 2
    while sheet_name in wb.sheetnames:
        sheet_name = f"{base[:28]}_{k}"; k += 1
    like = next((wb[n] for n in wb.sheetnames if n.endswith("_Positive") and n != sheet_name), None)
    ws = wb.create_sheet(sheet_name)
    _style_header(ws, STEP_HEADERS, like)
    for r in rows:
        ws.append(r)
    for col, w in zip("ABCDEFG", [13, 24, 14, 70, 34, 20, 42]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    # the lst row was set only after case name is known; also tell which sheet to run
    wb.save(out_path)
    return out_path, warnings


def export_from_json(json_path, test_data_path, out_path, cfg):
    tc = json.loads(Path(json_path).read_text(encoding="utf-8"))
    td = json.loads(Path(test_data_path).read_text(encoding="utf-8")) if Path(test_data_path).exists() else {}
    return export_master(tc, td, out_path, cfg)