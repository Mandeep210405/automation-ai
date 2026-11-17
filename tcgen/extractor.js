// extractor.js — runs INSIDE the browser through frame.evaluate(code, opts).
//
// One single pass over the live DOM (iterative, no recursion) that:
//   * skips whole subtrees that can never matter (script, style, svg, display:none ...)
//   * Layer 1: finds interactive candidates using several signals
//   * Layer 2: attaches context to each candidate (label, heading, section, form,
//              dialog, table row/column, description, validation state)
//   * collects page-level context (headings, messages, dialogs, tables, text)
//   * tags every kept element with data-tcg-ref="<unique ref>" so Python can
//     find exactly that element again and validate locators against it.
//
// Cost is O(number of elements). Python never parses the raw HTML, so a
// 300k-line DOM stays fast: only the compact result crosses into Python.
(opts) => {
  opts = opts || {};
  const REF_ATTR = 'data-tcg-ref';
  const PREFIX = opts.capturePrefix || 'c0f0';
  const MAX_ELEMENTS = opts.maxElements || 4000;
  const MAX_TEXT_BLOCKS = opts.maxTextBlocks || 400;
  const INCLUDE_HIDDEN = !!opts.includeHidden;

  const SKIP = new Set(['script', 'style', 'noscript', 'template', 'svg', 'head', 'meta', 'link',
    'iframe', 'frame', 'object', 'embed', 'canvas', 'math', 'br', 'hr', 'wbr', 'source', 'track']);
  const NATIVE = new Set(['a', 'button', 'input', 'select', 'textarea', 'summary']);
  const ROLES = new Set(['button', 'link', 'textbox', 'checkbox', 'radio', 'combobox', 'listbox',
    'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'switch', 'slider', 'searchbox',
    'option', 'spinbutton', 'treeitem']);
  const CONTAINER_TAGS = new Set(['section', 'article', 'aside', 'nav', 'main', 'header', 'footer',
    'form', 'fieldset', 'dialog']);
  const CONTAINER_ROLES = new Set(['dialog', 'alertdialog', 'region', 'navigation', 'tabpanel',
    'group', 'form', 'search', 'main', 'banner', 'contentinfo', 'complementary', 'menu',
    'listbox', 'tablist', 'toolbar', 'grid', 'table', 'tree']);
  const STRONG = new Set(['native', 'role', 'handler', 'contenteditable']);
  const INTERACTIVE_ANCESTOR = 'a[href],button,select,textarea,summary,[role="button"],[role="link"],' +
    '[role="menuitem"],[role="tab"],[role="option"],[role="checkbox"],[role="radio"],[role="switch"],[role="treeitem"]';
  const MESSAGE_CLASS = /(^|[\s_-])(error|errors|invalid|alert|toast|warning|success|notification|snackbar|feedback|helper-text|validation)([\s_-]|$)/i;
  const TESTID_ATTRS = ['data-testid', 'data-test-id', 'data-test', 'data-cy', 'data-qa'];

  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const cut = (t, n) => { t = clean(t); return t.length > n ? t.slice(0, n - 1) + '…' : t; };

  // Remove refs from any previous capture (light DOM). Shadow DOM refs are
  // removed while walking. Refs also carry a capture prefix, so a stale one
  // can never collide with a new one.
  try { document.querySelectorAll('[' + REF_ATTR + ']').forEach((e) => e.removeAttribute(REF_ATTR)); } catch (e) { /* ignore */ }

  const vw = window.innerWidth, vh = window.innerHeight;

  function prunable(el) {
    // checkVisibility() is fast; false means no box (display:none on self or
    // ancestor, or display:contents). Only prune for real display:none.
    if (el.checkVisibility) {
      try { if (el.checkVisibility()) return false; } catch (e) { /* fall through */ }
    }
    return getComputedStyle(el).display === 'none';
  }

  function isVisibleBox(el, cs) {
    if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function byIdIn(el, id) {
    const root = el.getRootNode ? el.getRootNode() : document;
    return (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
  }

  function textOfIds(el, ids) {
    return clean((ids || '').split(/\s+/).map((i) => (i ? byIdIn(el, i) : null))
      .filter(Boolean).map((n) => n.textContent).join(' '));
  }

  function labelText(l) {
    if (!l.querySelector('select,textarea')) return l.textContent;
    const c = l.cloneNode(true);
    c.querySelectorAll('select,textarea').forEach((n) => n.remove());
    return c.textContent;
  }

  function roleOf(el, tag, explicit) {
    if (explicit) return explicit;
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'search') return 'searchbox';
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      if (t === 'file') return 'button';
      return 'textbox';
    }
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    if (el.isContentEditable) return 'textbox';
    return null;
  }

  function nameOf(el, tag) {
    let v = clean(el.getAttribute('aria-label'));
    if (v) return cut(v, 100);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { v = textOfIds(el, lb); if (v) return cut(v, 100); }
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (el.labels && el.labels.length) {
        v = clean([...el.labels].map(labelText).join(' '));
        if (v) return cut(v, 100);
      }
      const t = (el.getAttribute('type') || '').toLowerCase();
      if (['submit', 'button', 'reset'].includes(t) && clean(el.value)) return cut(el.value, 100);
      if (t === 'image' && clean(el.getAttribute('alt'))) return cut(el.getAttribute('alt'), 100);
      return cut(el.getAttribute('placeholder') || el.getAttribute('title') || '', 100);
    }
    v = clean(el.textContent);
    if (v) return cut(v, 100);
    const img = el.querySelector('img[alt]');
    if (img && clean(img.getAttribute('alt'))) return cut(img.getAttribute('alt'), 100);
    const st = el.querySelector('svg title');
    if (st && clean(st.textContent)) return cut(st.textContent, 100);
    return cut(el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('data-placeholder') || '', 100);
  }

  function containerLabel(el) {
    let v = clean(el.getAttribute('aria-label'));
    if (v) return cut(v, 80);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { v = textOfIds(el, lb); if (v) return cut(v, 80); }
    const legend = el.localName === 'fieldset' ? el.querySelector(':scope > legend') : null;
    if (legend && clean(legend.textContent)) return cut(legend.textContent, 80);
    const cap = el.querySelector(':scope > caption');
    if (cap && clean(cap.textContent)) return cut(cap.textContent, 80);
    const h = el.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
    if (h && clean(h.textContent)) return cut(h.textContent, 80);
    return '';
  }

  function signalsOf(el, tag, explicitRole, cursor, parentCursor) {
    const s = [];
    if (NATIVE.has(tag)) {
      if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') return s;
      if (!(tag === 'a' && !el.hasAttribute('href'))) s.push('native');
    }
    if (explicitRole && ROLES.has(explicitRole)) s.push('role');
    const ti = el.getAttribute('tabindex');
    if (ti !== null && ti !== '-1') s.push('tabindex');
    if (el.hasAttribute('onclick') || el.hasAttribute('ng-click') || el.hasAttribute('jsaction') ||
        el.hasAttribute('data-action') || el.hasAttribute('v-on:click')) s.push('handler');
    if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) s.push('contenteditable');
    if (cursor === 'pointer' && parentCursor !== 'pointer') s.push('pointer');
    if (!s.length) return s;
    const strong = s.some((x) => STRONG.has(x));
    // Weak signals inside an existing control = the icon/span inside a button.
    if (!strong && el.parentElement && el.parentElement.closest(INTERACTIVE_ANCESTOR)) return [];
    return s;
  }

  function cssPath(el) {
    for (const a of TESTID_ATTRS) {
      const v = el.getAttribute(a);
      if (v) return '[' + a + '="' + CSS.escape(v) + '"]';
    }
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.localName !== 'body' && cur.localName !== 'html') {
      if (cur.id) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      let part = cur.localName;
      const parent = cur.parentElement;
      if (parent) {
        const same = [...parent.children].filter((c) => c.localName === cur.localName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  }

  // ---- table helpers (cached) -------------------------------------------
  const rowTextCache = new Map();
  const headerCache = new Map();
  function rowText(tr) {
    if (!tr) return '';
    if (!rowTextCache.has(tr)) {
      const cells = [...tr.children].map((c) => cut(c.textContent, 40)).filter(Boolean);
      rowTextCache.set(tr, cut(cells.join(' | '), 140));
    }
    return rowTextCache.get(tr);
  }
  function tableHeaders(table) {
    if (!headerCache.has(table)) {
      const hs = [...table.querySelectorAll('th,[role="columnheader"]')].slice(0, 30).map((h) => cut(h.textContent, 40));
      headerCache.set(table, hs);
    }
    return headerCache.get(table);
  }
  function columnOf(td) {
    if (!td || !td.parentElement) return '';
    const table = td.closest('table,[role="grid"],[role="table"],[role="treegrid"]');
    if (!table) return '';
    const idx = [...td.parentElement.children].indexOf(td);
    return tableHeaders(table)[idx] || '';
  }

  function headingFor(el) {
    if (!lastHeading) return '';
    if (lastHeadingScope && !lastHeadingScope.contains(el)) return '';
    return lastHeading;
  }

  // ---- outputs ------------------------------------------------------------
  const elements = [];
  const headings = [];
  const messages = [];
  const dialogs = [];
  const tables = [];
  const textBlocks = [];
  const seenMsg = new Set();
  let breadcrumbs = '';
  let lastHeading = '';
  let lastHeadingScope = null;   // a heading only describes elements inside its own section
  const SCOPE_SEL = 'section,article,aside,nav,main,header,footer,form,fieldset,dialog,[role="dialog"],' +
    '[role="alertdialog"],[role="region"],[role="tabpanel"],[role="form"]';
  let visited = 0, prunedHidden = 0, truncated = false, idx = 0;

  const rootCtx = { section: '', form: '', dialog: '', tr: null, td: null };
  const start = document.body || document.documentElement;
  const stack = [[start, rootCtx, 'auto']];

  while (stack.length) {
    const [el, pctx, parentCursor] = stack.pop();
    const tag = el.localName;
    if (!tag || SKIP.has(tag)) continue;
    visited++;
    if (el.hasAttribute(REF_ATTR)) el.removeAttribute(REF_ATTR);
    if (prunable(el)) { prunedHidden++; continue; }

    const cs = getComputedStyle(el);
    const explicitRole = (el.getAttribute('role') || '').split(/\s+/)[0].toLowerCase();
    let ctx = pctx;

    // ---- Layer 2: containers (section / form / dialog / nav) ----------------
    const isDialog = tag === 'dialog' || explicitRole === 'dialog' || explicitRole === 'alertdialog' ||
      el.getAttribute('aria-modal') === 'true';
    if (CONTAINER_TAGS.has(tag) || CONTAINER_ROLES.has(explicitRole) || isDialog) {
      const lbl = containerLabel(el);
      ctx = Object.assign({}, ctx);
      if (lbl) ctx.section = lbl;
      if (tag === 'form' || explicitRole === 'form' || explicitRole === 'search') {
        ctx.form = lbl || el.getAttribute('name') || el.id || (explicitRole === 'search' ? 'search' : 'form');
      }
      if (isDialog && isVisibleBox(el, cs)) {
        ctx.dialog = lbl || 'dialog';
        dialogs.push({ label: ctx.dialog, text: cut(el.innerText || el.textContent, 300) });
      }
      if (/breadcrumb/i.test(lbl + ' ' + (el.className || '')) && !breadcrumbs) {
        breadcrumbs = cut(el.textContent, 200);
      }
    }
    if (tag === 'tr' || explicitRole === 'row') ctx = Object.assign({}, ctx, { tr: el });
    if (tag === 'td' || tag === 'th' || explicitRole === 'cell' || explicitRole === 'gridcell') ctx = Object.assign({}, ctx, { td: el });

    // ---- Layer 2: page-level context ---------------------------------------
    if (/^h[1-6]$/.test(tag) || explicitRole === 'heading') {
      const t = cut(el.textContent, 120);
      if (t && isVisibleBox(el, cs)) {
        lastHeading = t;
        lastHeadingScope = (el.parentElement && el.parentElement.closest(SCOPE_SEL)) || null;
        if (headings.length < 60) {
          const level = /^h[1-6]$/.test(tag) ? Number(tag[1]) : Number(el.getAttribute('aria-level') || 2);
          headings.push({ level, text: t });
        }
      }
    }
    const live = el.getAttribute('aria-live');
    const cls = typeof el.className === 'string' ? el.className : '';
    if (explicitRole === 'alert' || explicitRole === 'status' || live === 'assertive' || live === 'polite' ||
        (MESSAGE_CLASS.test(cls) && el.children.length <= 3)) {
      const t = cut(el.textContent, 300);
      if (t.length >= 2 && !seenMsg.has(t) && messages.length < 30 && isVisibleBox(el, cs)) {
        seenMsg.add(t);
        messages.push({ kind: explicitRole || (live ? 'live' : 'class:' + (cls.match(MESSAGE_CLASS) || [])[2]), text: t });
      }
    }
    if ((tag === 'table' || explicitRole === 'grid' || explicitRole === 'table' || explicitRole === 'treegrid') && tables.length < 10) {
      const rows = [...el.querySelectorAll('tbody tr,[role="row"]')];
      const headers = tableHeaders(el);
      tables.push({
        label: containerLabel(el) || headingFor(el),
        columns: headers,
        row_count: rows.length,
        sample_rows: rows.filter((r) => !r.querySelector('th')).slice(0, 3).map(rowText),
      });
    }
    if ((tag === 'p' || tag === 'legend' || tag === 'figcaption' || tag === 'small' || tag === 'blockquote' ||
         tag === 'dd' || tag === 'dt' || (tag === 'label' && !el.control)) && textBlocks.length < MAX_TEXT_BLOCKS) {
      const t = cut(el.textContent, 300);
      if (t.length >= 12 && isVisibleBox(el, cs)) textBlocks.push({ text: t, heading: headingFor(el), section: ctx.section });
    }

    // ---- Layer 1: interactive candidates -----------------------------------
    const sig = signalsOf(el, tag, explicitRole, cs.cursor, parentCursor);
    if (sig.length) {
      if (elements.length >= MAX_ELEMENTS) {
        truncated = true;
      } else {
        const rect = el.getBoundingClientRect();
        const visible = isVisibleBox(el, cs);
        const pointerOnly = sig.length === 1 && sig[0] === 'pointer';
        // pointer-only junk: labels, huge wrappers, or a styled wrapper around a real control
        const junk = pointerOnly && (tag === 'label' || tag === 'body' || rect.width * rect.height > 0.4 * vw * vh ||
          !!el.querySelector('input:not([type="hidden"]),select,textarea,button,a[href]'));
        if (!junk && (visible || INCLUDE_HIDDEN)) {
          const ref = PREFIX + 'e' + (++idx);
          el.setAttribute(REF_ATTR, ref);
          const role = roleOf(el, tag, explicitRole);
          const rec = { ref, tag, role, name: nameOf(el, tag), signals: sig, visible };
          rec.in_viewport = rect.bottom > 0 && rect.right > 0 && rect.top < vh && rect.left < vw;
          const add = (k, v) => { if (v !== null && v !== undefined && v !== '' && v !== false) rec[k] = v; };
          add('type', el.getAttribute('type'));
          add('placeholder', cut(el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '', 80));
          if (tag === 'a') add('href', cut(el.getAttribute('href') || '', 120));
          add('id', el.id);
          add('name_attr', el.getAttribute('name'));
          for (const a of TESTID_ATTRS) {
            const v = el.getAttribute(a);
            if (v) { rec.testid = v; rec.testid_attr = a; break; }
          }
          if (tag === 'select') {
            const opt = el.options[el.selectedIndex];
            add('value', opt ? cut(opt.textContent, 60) : '');
            add('options', [...el.options].slice(0, 25).map((o) => cut(o.textContent, 40)));
            if (el.options.length > 25) rec.options_total = el.options.length;
          } else if ((tag === 'input' || tag === 'textarea') && (el.type || '').toLowerCase() !== 'password') {
            const t = (el.type || '').toLowerCase();
            if (!['checkbox', 'radio', 'submit', 'button', 'reset', 'file', 'image'].includes(t)) add('value', cut(el.value, 60));
          }
          const checkedAttr = el.getAttribute('aria-checked');
          if (tag === 'input' && ['checkbox', 'radio'].includes((el.type || '').toLowerCase())) rec.checked = !!el.checked;
          else if (checkedAttr !== null) rec.checked = checkedAttr === 'true';
          add('disabled', !!el.disabled || el.getAttribute('aria-disabled') === 'true');
          add('required', !!el.required || el.getAttribute('aria-required') === 'true');
          add('readonly', !!el.readOnly || el.getAttribute('aria-readonly') === 'true');
          const exp = el.getAttribute('aria-expanded');
          if (exp !== null) rec.expanded = exp === 'true';
          const selAttr = el.getAttribute('aria-selected');
          if (selAttr !== null) rec.selected = selAttr === 'true';
          let invalid = el.getAttribute('aria-invalid') === 'true';
          if (!invalid && (tag === 'input' || tag === 'select' || tag === 'textarea')) {
            try { invalid = el.matches(':user-invalid'); } catch (e) { /* older browser */ }
          }
          add('invalid', invalid);
          const desc = textOfIds(el, (el.getAttribute('aria-describedby') || '') + ' ' + (el.getAttribute('aria-errormessage') || ''));
          add('description', cut(desc, 160));
          add('heading', headingFor(el));
          add('section', ctx.section);
          add('form', ctx.form);
          add('dialog', ctx.dialog);
          if (ctx.tr) add('row', rowText(ctx.tr));
          if (ctx.td) add('column', columnOf(ctx.td));
          rec.css = cssPath(el);
          elements.push(rec);
        }
      }
    }

    // ---- children (reverse push keeps document order) ------------------------
    const kids = el.children;
    for (let i = kids.length - 1; i >= 0; i--) stack.push([kids[i], ctx, cs.cursor]);
    if (el.shadowRoot) {
      const sk = el.shadowRoot.children;
      for (let i = sk.length - 1; i >= 0; i--) stack.push([sk[i], ctx, cs.cursor]);
    }
  }

  const active = document.activeElement;
  let rawChars = null;
  try { rawChars = document.documentElement.outerHTML.length; } catch (e) { /* ignore */ }

  return {
    page: {
      url: location.href,
      title: document.title,
      viewport: { width: vw, height: vh },
      scroll_height: document.documentElement.scrollHeight,
      focused_ref: active && active.getAttribute ? active.getAttribute(REF_ATTR) : null,
    },
    stats: {
      dom_elements: document.getElementsByTagName('*').length,
      raw_html_chars: rawChars,
      visited,
      pruned_hidden_subtrees: prunedHidden,
      kept_interactive: elements.length,
      truncated,
    },
    elements,
    context: { headings, messages, dialogs, tables, text_blocks: textBlocks, breadcrumbs },
  };
}
