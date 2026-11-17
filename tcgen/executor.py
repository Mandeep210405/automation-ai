"""
executor.py — the EXECUTOR ENGINE box of the diagram.

For every target element it builds several locator candidates (test id, id,
role+name, label, placeholder, name attribute, CSS path) and VALIDATES them:
a candidate is accepted only if it matches exactly ONE element AND that element
is the one the AI picked (we tagged it with data-tcg-ref during capture).
The accepted locator is then used to perform the action, so a step that passes
is proof that the recorded locator works.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import expect

from .dom_filter import is_generated_id
from .prompts import placeholders

PW_ROLES = {"button", "link", "textbox", "checkbox", "radio", "combobox", "listbox", "menuitem",
            "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider", "searchbox", "option",
            "spinbutton", "treeitem"}

XPATH_JS = r"""
(el) => {
  const lit = s => !s.includes("'") ? "'" + s + "'" : (!s.includes('"') ? '"' + s + '"'
      : "concat('" + s.split("'").join("',\"'\",'") + "')");
  const snap = x => { try { return document.evaluate(x, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null); }
                       catch (e) { return null; } };
  const tag = el.tagName.toLowerCase();
  const generated = v => /\d{3,}|^:r|^radix|^headlessui|^mui|^react|^rc[-_]|^ember/i.test(v);
  const cands = [];
  const id = el.getAttribute('id');
  if (id && !generated(id)) cands.push(`//${tag}[@id=${lit(id)}]`);
  for (const a of ['data-testid', 'data-test', 'data-cy', 'data-qa', 'name', 'placeholder', 'aria-label', 'title']) {
    const v = el.getAttribute(a);
    if (v && v.length < 100 && !generated(v)) cands.push(`//${tag}[@${a}=${lit(v)}]`);
  }
  const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  if (text && text.length <= 80) {
    cands.push(`//${tag}[normalize-space()=${lit(text)}]`);
    if (text.length > 25) cands.push(`//${tag}[contains(normalize-space(), ${lit(text.slice(0, 25))})]`);
  }
  for (const x of cands) {
    const r = snap(x);
    if (r && r.snapshotLength === 1 && r.snapshotItem(0) === el) return {xpath: x, quality: 'stable'};
  }
  for (const x of cands) {
    const r = snap(x);
    if (!r) continue;
    for (let i = 0; i < r.snapshotLength && i < 20; i++)
      if (r.snapshotItem(i) === el) return {xpath: `(${x})[${i + 1}]`, quality: 'indexed'};
  }
  const parts = [];
  for (let n = el; n && n.nodeType === 1; n = n.parentNode) {
    let i = 1;
    for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
    parts.unshift(n.tagName.toLowerCase() + '[' + i + ']');
    if (n.tagName === 'HTML') break;
  }
  return {xpath: '/' + parts.join('/'), quality: 'positional'};
}
"""

def _attr(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def _lit(v) -> str:
    return json.dumps(v, ensure_ascii=False)


@dataclass
class Candidate:
    kind: str
    args: dict = field(default_factory=dict)
    quality: str = "high"

    def build(self, base):
        a = self.args
        if self.kind == "role":
            return base.get_by_role(a["role"], name=a["name"], exact=True)
        if self.kind == "label":
            return base.get_by_label(a["text"], exact=True)
        if self.kind == "placeholder":
            return base.get_by_placeholder(a["text"], exact=True)
        return base.locator(a["selector"])

    def code(self, base_expr: str = "page") -> str:
        a = self.args
        if self.kind == "role":
            return f"{base_expr}.get_by_role({_lit(a['role'])}, name={_lit(a['name'])}, exact=True)"
        if self.kind == "label":
            return f"{base_expr}.get_by_label({_lit(a['text'])}, exact=True)"
        if self.kind == "placeholder":
            return f"{base_expr}.get_by_placeholder({_lit(a['text'])}, exact=True)"
        return f"{base_expr}.locator({_lit(a['selector'])})"


def candidates_for(el: dict) -> list[Candidate]:
    c = []
    name = el.get("name") or ""
    full_name = name and not name.endswith("…")
    if el.get("testid"):
        c.append(Candidate("testid", {"selector": f'[{el.get("testid_attr", "data-testid")}="{_attr(el["testid"])}"]'}))
    if el.get("id") and not is_generated_id(el["id"]):
        sel = f'#{el["id"]}' if re.fullmatch(r"[A-Za-z][\w-]*", el["id"]) else f'[id="{_attr(el["id"])}"]'
        c.append(Candidate("id", {"selector": sel}))
    if el.get("role") in PW_ROLES and full_name:
        c.append(Candidate("role", {"role": el["role"], "name": name}))
    if el["tag"] in ("input", "select", "textarea") and full_name:
        c.append(Candidate("label", {"text": name}))
    if el.get("placeholder"):
        c.append(Candidate("placeholder", {"text": el["placeholder"]}, "medium"))
    if el.get("name_attr"):
        c.append(Candidate("name_attr", {"selector": f'{el["tag"]}[name="{_attr(el["name_attr"])}"]'}, "medium"))
    if el.get("css"):
        c.append(Candidate("css", {"selector": el["css"]}, "low"))
    return c


class StepError(Exception):
    pass


def _short_error(exc: Exception) -> str:
    text = str(exc)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = lines[:1] + [ln for ln in lines[1:] if any(k in ln for k in (
        "intercepts pointer", "not visible", "not enabled", "not editable", "detached", "resolved to",
        "Expected", "Received", "strict mode", "outside of the viewport", "did not match"))][:4]
    return " | ".join(keep)[:500] or exc.__class__.__name__


class Executor:
    def __init__(self, context, page, cfg, test_data: dict):
        self.context = context
        self.page = page
        self.cfg = cfg
        self.test_data = test_data

    # ---------------------------------------------------------------- helpers
    def substitute(self, text: str) -> str:
        for key in placeholders(text):
            text = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", lambda _m: str(self.test_data[key]), text)
        return text

    def _frame_for(self, el: dict, snap):
        idx = el.get("frame", 0)
        if idx == 0:
            return self.page, "page"
        want = next((f["url"] for f in snap.frames if f["index"] == idx), None)
        frames = self.page.frames
        frame = frames[idx] if idx < len(frames) and frames[idx].url == want else None
        if frame is None:
            frame = next((f for f in frames if f.url == want), None)
        if frame is None:
            raise StepError(f"The iframe that contained {el['sid']} is gone (page changed).")
        base_url = (want or "").split("?")[0]
        return frame, f"page.frame(url=lambda u: u.split('?')[0] == {_lit(base_url)})"

    def resolve(self, el: dict, snap):
        """Return (candidate, locator, base_expr). Raises StepError if element is gone."""
        frame, base_expr = self._frame_for(el, snap)
        ref_loc = frame.locator(f'[data-tcg-ref="{el["ref"]}"]')
        if ref_loc.count() == 0:
            raise StepError(f"{el['sid']} is no longer on the page (the page changed after the snapshot).")
        for cand in candidates_for(el):
            try:
                loc = cand.build(frame)
                if loc.count() != 1:
                    continue
                if loc.evaluate("(e, r) => e.getAttribute('data-tcg-ref') === r", el["ref"]):
                    return cand, loc, base_expr
            except PWError:
                continue
        # No stable unique locator: execute through the capture tag, record CSS path, flag it.
        fallback = Candidate("css", {"selector": el.get("css") or f'[data-tcg-ref="{el["ref"]}"]'}, "unverified")
        return fallback, ref_loc, base_expr

    def settle(self):
        for state, timeout in (("domcontentloaded", 5000), ("networkidle", self.cfg.SETTLE_NETWORKIDLE_MS)):
            try:
                self.page.wait_for_load_state(state, timeout=timeout)
            except (PWTimeout, PWError):
                pass
        self.page.wait_for_timeout(self.cfg.SETTLE_EXTRA_MS)

    def screenshot(self, path) -> str | None:
        try:
            self.page.screenshot(path=str(path), timeout=10_000)
            return str(path)
        except (PWTimeout, PWError):
            return None

    # ---------------------------------------------------------------- main
    def execute(self, action: dict, snap) -> dict:
        """Run one validated action. Returns a result dict with ok=True/False."""
        name = action["action"]
        value_tpl = action.get("value", "")
        value = self.substitute(value_tpl)
        el = snap.get(action.get("target", "")) if action.get("target") else None
        T, A = self.cfg.ACTION_TIMEOUT_MS, self.cfg.ASSERT_TIMEOUT_MS
        result = {"ok": False, "action": name, "value_template": value_tpl, "element": el,
                  "locator_code": None, "locator_quality": None, "base_expr": "page", "new_page": False}
        pages_before = list(self.context.pages)
        try:
            loc = None
            if el:
                cand, loc, base_expr = self.resolve(el, snap)
                result["base_expr"] = base_expr
                result["locator_code"] = cand.code(base_expr)
                result["locator_quality"] = cand.quality
                result["locator_kind"] = cand.kind
                try:
                    xp = loc.evaluate(XPATH_JS)
                    result["xpath"], result["xpath_quality"] = xp["xpath"], xp["quality"]
                    result["frame_note"] = base_expr != "page"
                except PWError:
                    result["xpath"], result["xpath_quality"] = "", ""

            if name == "skip":
                pass
            elif name == "click":
                loc.click(timeout=T)
            elif name == "fill":
                loc.fill(value, timeout=T)
            elif name == "select_option":
                try:
                    loc.select_option(label=value, timeout=T)
                except (PWTimeout, PWError):
                    loc.select_option(value=value, timeout=T)
            elif name == "check":
                loc.check(timeout=T)
            elif name == "uncheck":
                loc.uncheck(timeout=T)
            elif name == "hover":
                loc.hover(timeout=T)
            elif name == "press":
                if loc is not None:
                    loc.press(value, timeout=T)
                else:
                    self.page.keyboard.press(value)
            elif name == "navigate":
                self.page.goto(value, wait_until="domcontentloaded", timeout=self.cfg.NAVIGATION_TIMEOUT_MS)
            elif name == "wait":
                self.page.wait_for_timeout(float(value) * 1000)
            elif name == "assert_visible":
                expect(loc).to_be_visible(timeout=A)
            elif name == "assert_not_visible":
                expect(loc).to_be_hidden(timeout=A)
            elif name == "assert_text_visible":
                expect(self.page.get_by_text(value).first).to_be_visible(timeout=A)
            elif name == "assert_url_contains":
                expect(self.page).to_have_url(re.compile(re.escape(value)), timeout=A)
            elif name == "assert_title_contains":
                expect(self.page).to_have_title(re.compile(re.escape(value)), timeout=A)
            elif name == "assert_value":
                expect(loc).to_have_value(value, timeout=A)
            elif name == "assert_enabled":
                expect(loc).to_be_enabled(timeout=A)
            elif name == "assert_disabled":
                expect(loc).to_be_disabled(timeout=A)
            else:
                raise StepError(f"Executor does not support action '{name}'")
        except StepError as exc:
            result["error"] = str(exc)
            return result
        except (PWTimeout, PWError, AssertionError, ValueError) as exc:
            result["error"] = _short_error(exc)
            return result

        # A click may open a new tab: continue the test in it.
        if name == "click":
            self.page.wait_for_timeout(300)
        new_pages = [p for p in self.context.pages if p not in pages_before]
        if new_pages:
            self.page = new_pages[-1]
            result["new_page"] = True
        if not name.startswith("assert") and name not in ("skip", "wait"):
            self.settle()
        result["ok"] = True
        return result
