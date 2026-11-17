"""
prompts.py — system prompts, JSON schemas and validation of AI output.
The AI may only choose from a FIXED list of actions; anything else is rejected
before it reaches the browser.
"""
from __future__ import annotations

import re

ACTIONS = [
    "click", "fill", "select_option", "check", "uncheck", "press", "hover", "navigate", "wait",
    "assert_visible", "assert_not_visible", "assert_text_visible", "assert_url_contains",
    "assert_title_contains", "assert_value", "assert_enabled", "assert_disabled", "skip",
]
TARGET_REQUIRED = {"click", "fill", "select_option", "check", "uncheck", "hover", "assert_visible",
                   "assert_not_visible", "assert_value", "assert_enabled", "assert_disabled"}
VALUE_REQUIRED = {"fill", "select_option", "press", "navigate", "wait", "assert_text_visible",
                  "assert_url_contains", "assert_title_contains", "assert_value"}

_STEP_ITEM = {
    "type": "object",
    "properties": {"description": {"type": "string"}, "expected_result": {"type": "string"}},
    "required": ["description", "expected_result"],
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "objective": {"type": "string"},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": _STEP_ITEM},
    },
    "required": ["title", "objective", "preconditions", "steps"],
}
REPLAN_SCHEMA = {
    "type": "object",
    "properties": {"remaining_steps": {"type": "array", "items": _STEP_ITEM}},
    "required": ["remaining_steps"],
}
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "action": {"type": "string", "enum": ACTIONS},
        "target": {"type": "string"},
        "value": {"type": "string"},
        "completes_step": {"type": "boolean"},
    },
    "required": ["reason", "action", "target", "value", "completes_step"],
}

PLANNER_SYSTEM = """You are a senior QA engineer who writes UI test cases for web applications.
You receive: the tester's request, a filtered snapshot of the START page (page context + interactive elements),
the login state, and the available test data keys. Write ONE test case as JSON.

Rules:
- Steps are atomic: each step is exactly ONE user action (click, type, select, check, press a key, navigate) OR ONE verification.
- Describe steps in plain language using the element's visible label, e.g.
  "Enter {{email}} in the 'Email' field", "Click the 'Login' button", "Verify the 'Dashboard' heading is visible".
- Later steps may refer to pages that appear after earlier actions (you only see the first page).
- For test data use {{key}} placeholders, ONLY with keys from the available list. Otherwise invent realistic sample values.
- If the user is already logged in, do NOT add login steps unless the request is about login.
- The browser is already on the start URL: do not add a step to open it.
- Every step needs a concrete expected_result. Include verification steps that prove the request's goal
  (positive or negative, as the tester asked).
- At most {max_steps} steps. Stay focused on the request.
Reply with ONLY this JSON shape, using exactly these key names:
{"title": "...", "objective": "...", "preconditions": ["..."],
 "steps": [{"description": "Click the 'Add Remark' button", "expected_result": "The add-remark form opens"}]}
Every step MUST have a non-empty "description" and a non-empty "expected_result"."""

STEP_SYSTEM = """You are a test automation engine. Convert the CURRENT test step into exactly ONE browser action,
using ONLY elements listed in the current page snapshot.

Allowed actions (target = element id like "e12", or "" when not needed):
- click(target)                       - fill(target, value)              - select_option(target, value=visible option text; native <select> only)
- check(target) / uncheck(target)     - press(value=key e.g. "Enter"; target optional)
- hover(target)                       - navigate(value=URL)              - wait(value=seconds, max 10)
- assert_visible(target)              - assert_not_visible(target)       - assert_text_visible(value=short distinctive text)
- assert_url_contains(value)          - assert_title_contains(value)     - assert_value(target, value)
- assert_enabled(target)              - assert_disabled(target)
- skip                                (only if the step is already satisfied on this page; explain in reason)

Rules:
- target MUST be an id that exists in the snapshot, written exactly like "e12".
- Choose the element whose name, label, placeholder, form, section, row or column best matches the step.
  If a dialog is open, the element is almost always inside the dialog.
- Custom dropdowns (combobox/button without options=...): click to open it (completes_step=false), then click the option next turn.
- For verifying messages or headings prefer assert_text_visible with a short distinctive part of the text.
- Keep {{key}} placeholders exactly as written in the step; never invent real secrets.
- completes_step = true when this action finishes the current step, false when more actions are needed for it.
- reason: one short sentence."""

REPAIR_EXTRA = """

You are now FIXING a failed attempt. Read the error, compare with the NEW snapshot (element ids were renumbered,
the page may have changed: dialogs, loading, validation errors, other page). Propose a DIFFERENT, corrected action.
Do not repeat an action+target that already failed. If something blocks the step (e.g. a popup/cookie banner),
you may first deal with it (completes_step=false)."""

REPLAN_SYSTEM = """You are a senior QA engineer maintaining a UI test case while it is being executed.
The page changed after the last successful action. Using the NEW page snapshot, return the remaining steps.
Keep steps that are still valid (unchanged wording if possible), adjust or add steps only when the new page requires it,
never repeat completed steps, keep steps atomic (one action or one verification) and keep {{key}} placeholders.
At most {max_steps} remaining steps."""


def plan_user_prompt(request: str, url: str, logged_in: bool, data_keys: list, page_text: str) -> str:
    keys = ", ".join(data_keys) if data_keys else "(none)"
    return (f"TESTER REQUEST:\n{request}\n\nSTART URL: {url}\nLOGIN STATE: "
            f"{'already logged in (session loaded)' if logged_in else 'not logged in / login not required'}\n"
            f"AVAILABLE TEST DATA KEYS: {keys}\n\nSTART PAGE SNAPSHOT:\n{page_text}\n\nReturn the test case JSON.")


def _plan_lines(steps: list, current: int) -> str:
    lines = []
    start = max(0, current - 5)
    if start > 0:
        lines.append(f"  ... {start} earlier steps done")
    for i in range(start, len(steps)):
        mark = "DONE" if i < current else ("CURRENT ->" if i == current else "todo")
        s = steps[i]
        line = f"  {mark} {i + 1}. {s['description']}"
        if i == current:
            line += f"  (expected: {s['expected_result']})"
        lines.append(line)
    return "\n".join(lines)


def step_user_prompt(title: str, request: str, steps: list, current: int, done_actions: list,
                     data_keys: list, page_text: str, failures: list | None = None) -> str:
    parts = [f"TEST CASE: {title}", f"GOAL: {request}", "PLAN:", _plan_lines(steps, current)]
    if done_actions:
        parts.append("ACTIONS ALREADY DONE FOR THE CURRENT STEP:")
        parts += [f"  - {a}" for a in done_actions]
    if failures:
        parts.append("FAILED ATTEMPTS FOR THE CURRENT STEP:")
        parts += [f"  - {f}" for f in failures]
    parts.append(f"TEST DATA KEYS: {', '.join(data_keys) if data_keys else '(none)'}")
    parts.append(f"CURRENT PAGE SNAPSHOT:\n{page_text}")
    parts.append("Return the JSON for the next action of the CURRENT step.")
    return "\n".join(parts)


def replan_user_prompt(title: str, request: str, done: list, remaining: list, page_text: str) -> str:
    d = "\n".join(f"  {i + 1}. {s['description']}" for i, s in enumerate(done)) or "  (none)"
    r = "\n".join(f"  - {s['description']} (expected: {s['expected_result']})" for s in remaining) or "  (none)"
    return (f"TEST CASE: {title}\nGOAL: {request}\nCOMPLETED STEPS:\n{d}\nREMAINING STEPS (old plan):\n{r}\n\n"
            f"NEW PAGE SNAPSHOT:\n{page_text}\n\nReturn remaining_steps JSON.")


# --------------------------------------------------------------------------- validation
_PLACEHOLDER = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


def placeholders(text: str) -> list[str]:
    return _PLACEHOLDER.findall(text or "")


def validate_plan(plan: dict, max_steps: int) -> str | None:
    raw = plan.get("steps") or plan.get("test_steps") or []
    steps = []
    for s in raw:
        if isinstance(s, str):
            s = {"description": s}
        if not isinstance(s, dict):
            continue
        desc = next((str(s[k]).strip() for k in ("description", "step", "action", "text", "name", "title")
                     if s.get(k) and str(s[k]).strip()), "")
        exp = next((str(s[k]).strip() for k in ("expected_result", "expected", "expected_outcome", "result", "verification")
                    if s.get(k) and str(s[k]).strip()), "")
        if desc:
            steps.append({"description": desc, "expected_result": exp or "Step completes without error"})
    if not steps:
        return "plan has no steps with a non-empty 'description'"
    plan["steps"] = steps[:max_steps]
    return None


def validate_action(action: dict, snap, data_keys: list) -> str | None:
    """Return an error message, or None if the action is acceptable."""
    name = action.get("action")
    if name not in ACTIONS:
        return f"unknown action '{name}'"
    target = (action.get("target") or "").strip()
    value = action.get("value")
    action["target"] = target
    action["value"] = "" if value is None else str(value)
    if name in TARGET_REQUIRED:
        if not target:
            return f"action '{name}' needs a target element id"
        el = snap.get(target)
        if not el:
            return f"target '{target}' does not exist in the current snapshot"
        if name == "select_option" and el["tag"] != "select":
            return (f"select_option only works on native <select>; {target} is <{el['tag']}> "
                    f"(role {el.get('role')}). Click it to open, then click the option.")
        if name in ("check", "uncheck") and el.get("role") not in ("checkbox", "radio", "switch", "menuitemcheckbox"):
            return f"{name} needs a checkbox/radio/switch; {target} has role {el.get('role')}. Use click instead."
    elif target and not snap.get(target):
        action["target"] = ""
    if name in VALUE_REQUIRED and not action["value"].strip():
        return f"action '{name}' needs a value"
    for key in placeholders(action["value"]):
        if key not in data_keys:
            return f"unknown test data key '{{{{{key}}}}}'. Available: {', '.join(data_keys) or 'none'}"
    if name == "wait":
        try:
            secs = float(action["value"])
        except ValueError:
            return "wait value must be a number of seconds"
        action["value"] = str(min(max(secs, 0.5), 10))
    return None
