"""
dom_filter.py — capture + filter + rank + render the page for the LLM.

Why this is fast on huge pages (e.g. 300k lines of HTML):
  1. All heavy work happens INSIDE the browser in one pass (extractor.js).
     Python never downloads or parses the raw HTML.
  2. Only a compact list of interactive elements + context comes back.
  3. Before each LLM call we RANK elements by relevance to the current step
     and send only what fits a fixed token budget (e.g. 3000 tokens),
     while always keeping: open dialogs, the focused element, and complete
     forms that contain relevant fields. So token use stays roughly constant
     no matter how big the page is.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

EXTRACTOR_JS = Path(__file__).with_name("extractor.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- snapshot
@dataclass
class Snapshot:
    capture_id: int
    url: str
    title: str
    elements: list
    context: dict
    stats: dict
    frames: list
    focused_sid: str | None = None
    _index: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._index = {e["sid"]: e for e in self.elements}

    def get(self, sid: str):
        return self._index.get((sid or "").strip())

    def to_json(self) -> dict:
        return {
            "capture_id": self.capture_id, "url": self.url, "title": self.title,
            "stats": self.stats, "frames": self.frames, "focused": self.focused_sid,
            "context": self.context, "elements": self.elements,
        }


def capture(page, capture_id: int, cfg) -> Snapshot:
    """Run extractor.js in the main frame and every child frame, merge results."""
    elements, frames_info = [], []
    context = {"headings": [], "messages": [], "dialogs": [], "tables": [], "text_blocks": [], "breadcrumbs": ""}
    stats = Counter()
    focused_sid = None
    page_meta = {"url": page.url, "title": ""}
    opts_base = {
        "maxElements": cfg.MAX_ELEMENTS,
        "maxTextBlocks": cfg.MAX_TEXT_BLOCKS,
        "includeHidden": cfg.INCLUDE_HIDDEN,
    }

    for fi, frame in enumerate(page.frames):
        if frame.is_detached():
            continue
        if fi > 0 and (not frame.url or frame.url == "about:blank"):
            continue
        try:
            data = frame.evaluate(EXTRACTOR_JS, {**opts_base, "capturePrefix": f"c{capture_id}f{fi}"})
        except Exception as exc:  # cross-origin hiccup, frame navigated away, etc.
            frames_info.append({"index": fi, "url": frame.url, "error": str(exc)[:200]})
            continue
        frames_info.append({"index": fi, "url": frame.url, "name": frame.name or ""})
        if fi == 0:
            page_meta = data["page"]
        for el in data["elements"]:
            el["frame"] = fi
            el["sid"] = f"e{len(elements) + 1}"
            if data["page"].get("focused_ref") == el["ref"]:
                focused_sid = el["sid"]
            elements.append(el)
        ctx = data["context"]
        for key in ("headings", "messages", "dialogs", "tables", "text_blocks"):
            items = ctx.get(key, [])
            if fi > 0:
                items = [{**i, "frame": fi} for i in items]
            context[key].extend(items)
        if ctx.get("breadcrumbs") and not context["breadcrumbs"]:
            context["breadcrumbs"] = ctx["breadcrumbs"]
        for k, v in data["stats"].items():
            if isinstance(v, bool):
                stats[k] = stats[k] or v
            elif isinstance(v, (int, float)):
                stats[k] += v

    return Snapshot(
        capture_id=capture_id,
        url=page_meta.get("url") or page.url,
        title=page_meta.get("title", ""),
        elements=elements,
        context=context,
        stats=dict(stats),
        frames=frames_info,
        focused_sid=focused_sid,
    )


def page_signature(snap: Snapshot) -> str:
    """URL (without query) + SPA hash route. Used to decide when to re-plan."""
    u = urlsplit(snap.url)
    sig = f"{u.netloc}{u.path}"
    if u.fragment.startswith("/"):
        sig += "#" + u.fragment
    return sig


# --------------------------------------------------------------------------- locator hygiene
_GENERATED_ID = re.compile(
    r"^:r[0-9a-z]+:$"                       # React useId
    r"|^(mui|radix|headlessui|react-select|rc|ember|ext-gen|j_id|yui|downshift|cdk|mat-|ng-)[-_:]?.*\d"
    r"|\d{4,}"                              # long numbers
    r"|^[a-f0-9]{8,}$"                      # hashes
    r"|[0-9a-f]{8}-[0-9a-f]{4}-"            # uuid
    r"|^[a-z]{1,3}\d{2,}$",                 # e.g. u123, id42
    re.I,
)


def is_generated_id(value: str | None) -> bool:
    return bool(value) and bool(_GENERATED_ID.search(value))


# --------------------------------------------------------------------------- relevance ranking
_GROUPS = [
    {"login", "signin", "sign", "log", "logon", "authenticate"},
    {"logout", "signout"},
    {"submit", "save", "ok", "confirm", "apply", "continue", "next", "proceed", "done", "send"},
    {"cancel", "close", "dismiss", "back"},
    {"search", "find", "filter", "query", "lookup"},
    {"delete", "remove", "trash", "discard"},
    {"add", "create", "new", "plus", "insert", "register"},
    {"edit", "update", "modify", "change", "rename"},
    {"email", "mail", "username", "user", "userid"},
    {"password", "pass", "passwd", "pwd"},
    {"menu", "hamburger", "navigation", "nav", "sidebar"},
    {"upload", "attach", "browse", "file"},
    {"download", "export"},
    {"view", "open", "detail", "show"},
]
_SYN = {}
for _g in _GROUPS:
    for _w in _g:
        _SYN.setdefault(_w, set()).update(_g - {_w})

_FILL_WORDS = {"enter", "type", "fill", "input", "write", "provide", "set"}
_CHECK_WORDS = {"check", "tick", "select", "choose", "toggle", "enable", "disable", "pick"}
_CLICK_WORDS = {"click", "press", "tap", "submit", "open", "go", "navigate"}


def tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _element_terms(el: dict) -> dict:
    """token -> weight (best field weight in which it appears)."""
    fields = [
        (el.get("name"), 3.0), (el.get("placeholder"), 2.0), (el.get("id"), 1.5),
        (el.get("name_attr"), 1.5), (el.get("testid"), 1.5), (el.get("description"), 1.0),
        (el.get("row"), 1.0), (el.get("column"), 1.0), (el.get("form"), 1.0),
        (el.get("dialog"), 1.0), (el.get("section"), 0.8), (el.get("heading"), 0.8),
        (el.get("value"), 0.8), (el.get("href"), 0.5), (el.get("role"), 0.5), (el.get("type"), 0.5),
        (" ".join(el.get("options", [])[:10]), 0.5),
    ]
    terms = {}
    for text, w in fields:
        for t in tokenize(text or ""):
            if terms.get(t, 0) < w:
                terms[t] = w
    return terms


def score_elements(snap: Snapshot, query: str) -> dict:
    q_tokens = tokenize(query)
    q_weights = {}
    for t in q_tokens:
        q_weights[t] = 1.0
    for t in q_tokens:
        for s in _SYN.get(t, ()):
            q_weights.setdefault(s, 0.6)
    q_set = set(q_tokens)

    docs = {el["sid"]: _element_terms(el) for el in snap.elements}
    n = max(len(docs), 1)
    df = Counter()
    for terms in docs.values():
        df.update(terms.keys())
    has_dialog = bool(snap.context.get("dialogs"))

    scores = {}
    for el in snap.elements:
        terms = docs[el["sid"]]
        s = 0.0
        for t, qw in q_weights.items():
            if t in terms:
                s += qw * terms[t] * (math.log((n + 1) / (df[t] + 0.5)) + 1.0)
        role = el.get("role") or ""
        if q_set & _FILL_WORDS and role in ("textbox", "searchbox", "combobox", "spinbutton"):
            s += 1.0
        if q_set & _CHECK_WORDS and role in ("checkbox", "radio", "switch", "combobox", "option", "listbox", "tab"):
            s += 1.0
        if q_set & _CLICK_WORDS and role in ("button", "link", "menuitem", "tab"):
            s += 0.3
        if el.get("visible"):
            s += 0.5
        if el.get("in_viewport"):
            s += 0.3
        if el.get("disabled"):
            s -= 0.5
        if el.get("signals") == ["pointer"]:
            s -= 0.5
        if not el.get("name") and not el.get("placeholder"):
            s -= 1.0
        if el["sid"] == snap.focused_sid:
            s += 0.5
        if has_dialog:
            s = s + 2.5 if el.get("dialog") else s * 0.5
        scores[el["sid"]] = s
    return scores


# --------------------------------------------------------------------------- rendering
def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def render_element(el: dict) -> str:
    parts = [el["sid"], el.get("role") or el["tag"]]
    if el.get("name"):
        parts.append(_q(el["name"]))
    if el.get("role") in (None, "textbox", "button") and el.get("type") not in (None, "text", "submit", "button"):
        parts.append(f"type={el['type']}")
    if el.get("placeholder") and el.get("placeholder") != el.get("name"):
        parts.append(f"placeholder={_q(el['placeholder'])}")
    if el.get("id") and not is_generated_id(el["id"]):
        parts.append(f"id={el['id']}")
    if el.get("name_attr"):
        parts.append(f"name={el['name_attr']}")
    if el.get("testid"):
        parts.append(f"testid={el['testid']}")
    if el.get("href") and not el["href"].startswith("javascript"):
        parts.append(f"href={el['href'][:60]}")
    if el.get("value"):
        parts.append(f"value={_q(el['value'])}")
    if el.get("options"):
        opts = el["options"][:8]
        more = (el.get("options_total") or len(el["options"])) - len(opts)
        parts.append("options=" + _q(" | ".join(opts)) + (f"+{more} more" if more > 0 else ""))
    flags = [k for k in ("disabled", "required", "readonly", "invalid") if el.get(k)]
    for k in ("checked", "expanded", "selected"):
        if k in el:
            flags.append(f"{k}={'yes' if el[k] else 'no'}")
    if not el.get("visible"):
        flags.append("HIDDEN")
    if el.get("frame"):
        flags.append(f"frame={el['frame']}")
    if flags:
        parts.append("(" + ", ".join(flags) + ")")
    ctx = []
    for key, label in (("dialog", "dialog"), ("form", "form"), ("section", "section")):
        if el.get(key):
            ctx.append(f"{label}: {el[key]}")
    if el.get("heading") and el.get("heading") not in (el.get("section"), el.get("name")):
        ctx.append(f"under: {el['heading']}")
    if el.get("row"):
        ctx.append(f"row: {el['row']}")
    if el.get("column"):
        ctx.append(f"col: {el['column']}")
    if el.get("description"):
        ctx.append(f"hint: {el['description']}")
    if ctx:
        parts.append("[" + " | ".join(ctx) + "]")
    return " ".join(parts)


def _render_context(snap: Snapshot, query: str, budget_chars: int) -> str:
    c = snap.context
    lines = [f"PAGE: {_q(snap.title)} — {snap.url}"]
    if c.get("breadcrumbs"):
        lines.append(f"BREADCRUMBS: {c['breadcrumbs']}")
    for d in c.get("dialogs", [])[:3]:
        lines.append(f"OPEN DIALOG {_q(d['label'])}: {d['text']}")
    if c.get("messages"):
        lines.append("MESSAGES / ALERTS:")
        lines += [f"  - {m['text']}" for m in c["messages"][:8]]
    if c.get("headings"):
        hs = " | ".join(f"h{h['level']} {h['text']}" for h in c["headings"][:15])
        lines.append(f"HEADINGS: {hs}")
    for t in c.get("tables", [])[:4]:
        cols = ", ".join(t["columns"][:12])
        line = f"TABLE {_q(t['label'])}: {t['row_count']} rows; columns [{cols}]"
        if t.get("sample_rows"):
            line += "; e.g. " + " // ".join(t["sample_rows"][:2])
        lines.append(line)
    text = "\n".join(lines)

    blocks = c.get("text_blocks", [])
    if blocks and len(text) < budget_chars:
        qt = set(tokenize(query))
        ranked = sorted(blocks, key=lambda b: -len(qt & set(tokenize(b["text"]))))
        picked, used = [], len(text)
        for b in ranked:
            line = f"  - {b['text']}"
            if used + len(line) > budget_chars or len(picked) >= 10:
                break
            picked.append(line)
            used += len(line) + 1
        if picked:
            text += "\nPAGE TEXT (most relevant):\n" + "\n".join(picked)
    return text[:budget_chars]


def render_for_llm(snap: Snapshot, query: str, budget_tokens: int) -> tuple[str, dict]:
    """Return (text for the LLM, stats). Size stays within budget_tokens (~4 chars/token)."""
    budget = budget_tokens * 4
    ctx_text = _render_context(snap, query, int(budget * 0.35))
    el_budget = budget - len(ctx_text) - 200

    lines = {el["sid"]: render_element(el) for el in snap.elements}
    total_chars = sum(len(v) + 1 for v in lines.values())

    if total_chars <= el_budget:
        chosen = {el["sid"] for el in snap.elements}
    else:
        scores = score_elements(snap, query)
        ranked = sorted(snap.elements, key=lambda e: -scores[e["sid"]])
        priority = []
        # 1) everything in an open dialog (it blocks the rest of the page)
        priority += [e for e in snap.elements if e.get("dialog")][:40]
        # 2) focused element
        if snap.focused_sid:
            priority.append(snap.get(snap.focused_sid))
        # 3) complete forms that contain one of the top-10 relevant elements
        top_forms = []
        for e in ranked[:10]:
            if e.get("form") and e["form"] not in top_forms and scores[e["sid"]] > 0:
                top_forms.append(e["form"])
        for f in top_forms[:2]:
            priority += [e for e in snap.elements if e.get("form") == f][:25]
        chosen, used = set(), 0
        for e in priority + ranked:
            if not e or e["sid"] in chosen:
                continue
            cost = len(lines[e["sid"]]) + 1
            if used + cost > el_budget:
                if e in priority:
                    continue
                break
            chosen.add(e["sid"])
            used += cost

    shown = [lines[e["sid"]] for e in snap.elements if e["sid"] in chosen]   # document order
    omitted = [e for e in snap.elements if e["sid"] not in chosen]
    header = f"INTERACTIVE ELEMENTS ({len(shown)} of {len(snap.elements)} shown, page order):"
    text = ctx_text + "\n" + header + "\n" + "\n".join(shown)
    if omitted:
        groups = Counter((e.get("role") or e["tag"], e.get("name") or "") for e in omitted)
        summary = ", ".join(f"{n}x {r} {_q(nm[:30])}" for (r, nm), n in groups.most_common(6))
        text += f"\nOMITTED (less relevant): {len(omitted)} elements, e.g. {summary}"
    stats = {
        "elements_total": len(snap.elements),
        "elements_shown": len(shown),
        "chars": len(text),
        "approx_tokens": len(text) // 4,
    }
    return text, stats
