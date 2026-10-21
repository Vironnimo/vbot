"""The document an Agent widget renders in: base styles plus the parent bridge.

``show_widget`` stores only the Agent's HTML. The server wraps it here on every
request, so all widgets get the current base styles and bridge. The bridge
reports the widget's state and height to the WebUI frame host
(``.vorch/plans/agent-widgets.md`` -> Shell document and bridge).
"""

from __future__ import annotations

import html
import re

# The WebUI's dark theme, as the plain names the Tool description promises.
_BASE_STYLE = """<style>
:root {
  color-scheme: dark;
  --bg: #15130f;
  --surface: #1e1b17;
  --surface-2: #26231e;
  --text: #f1ede6;
  --muted: #b3aa9e;
  --border: #423c35;
  --accent: #e8870a;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
  font-size: 14px;
  line-height: 1.5;
  color: var(--text);
  background: transparent;
}
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; padding: 12px; }
input, select, textarea, button { font: inherit; color: inherit; }
input:not([type=range], [type=checkbox], [type=radio], [type=color]), select, textarea {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
}
button {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 12px;
  cursor: pointer;
}
button:hover { border-color: var(--accent); }
input[type=range], input[type=checkbox], input[type=radio] { accent-color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
a { color: var(--accent); }
</style>"""

# Defines ``window.vbot`` before any widget script runs. Messages go only to
# the parent frame host, which checks their source window and nonce.
_BRIDGE_SCRIPT = """<script>
(() => {
  'use strict';
  const SKIPPED = new Set(['button', 'submit', 'reset', 'image', 'file', 'password']);
  let nonce = null;
  let custom = {};
  let initialSent = false;
  let scheduled = false;
  let lastHeight = -1;
  const post = (message) => {
    try { window.parent.postMessage(message, '*'); } catch (_error) { /* detached */ }
  };
  const plain = (value) => {
    try { return JSON.parse(JSON.stringify(value)); } catch (_error) { return undefined; }
  };
  const controls = () => Array.from(
    document.querySelectorAll('input[name], select[name], textarea[name]')
  ).filter((el) => el.name && !(el.tagName === 'INPUT' && SKIPPED.has(el.type)));
  const single = (el) => {
    if (el.tagName === 'SELECT' && el.multiple) {
      return Array.from(el.selectedOptions, (option) => option.value);
    }
    if (el.tagName === 'INPUT' && (el.type === 'range' || el.type === 'number')) {
      return Number.isFinite(el.valueAsNumber) ? el.valueAsNumber : el.value;
    }
    return el.value;
  };
  const groupValue = (els) => {
    const first = els[0];
    if (first.tagName === 'INPUT' && first.type === 'radio') {
      const checked = els.find((el) => el.checked);
      return checked ? checked.value : null;
    }
    if (first.tagName === 'INPUT' && first.type === 'checkbox') {
      if (els.length === 1) return first.checked;
      return els.filter((el) => el.checked).map((el) => el.value);
    }
    const values = els.map(single);
    const same = values.every((value) => JSON.stringify(value) === JSON.stringify(values[0]));
    return same ? values[0] : values;
  };
  const collect = () => {
    const groups = new Map();
    for (const el of controls()) {
      if (!groups.has(el.name)) groups.set(el.name, []);
      groups.get(el.name).push(el);
    }
    const state = {};
    for (const [name, els] of groups) state[name] = groupValue(els);
    return Object.assign(state, custom);
  };
  const report = () => {
    scheduled = false;
    if (nonce === null) return;
    post({ type: 'vbot-widget-state', nonce, state: collect(), initial: !initialSent });
    initialSent = true;
  };
  const schedule = () => {
    if (nonce === null || scheduled) return;
    scheduled = true;
    requestAnimationFrame(report);
  };
  const restoreField = (els, value) => {
    for (const el of els) {
      if (el.tagName === 'INPUT' && el.type === 'radio') {
        el.checked = el.value === String(value);
      } else if (el.tagName === 'INPUT' && el.type === 'checkbox') {
        el.checked = Array.isArray(value) ? value.map(String).includes(el.value) : Boolean(value);
      } else if (el.tagName === 'SELECT' && el.multiple && Array.isArray(value)) {
        const chosen = value.map(String);
        for (const option of el.options) option.selected = chosen.includes(option.value);
      } else if (value !== null && value !== undefined) {
        el.value = String(Array.isArray(value) ? value[els.indexOf(el)] ?? value[0] : value);
      }
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };
  const restore = (state) => {
    const groups = new Map();
    for (const el of controls()) {
      if (!groups.has(el.name)) groups.set(el.name, []);
      groups.get(el.name).push(el);
    }
    for (const [key, value] of Object.entries(state)) {
      if (groups.has(key)) restoreField(groups.get(key), value);
      else custom[key] = value;
    }
    window.dispatchEvent(new CustomEvent('vbot-restore', { detail: plain(state) }));
  };
  const measure = () => {
    if (nonce === null || !document.body) return;
    const style = getComputedStyle(document.body);
    const height = Math.ceil(
      document.body.offsetHeight + parseFloat(style.marginTop) + parseFloat(style.marginBottom)
    );
    if (height === lastHeight) return;
    lastHeight = height;
    post({ type: 'vbot-widget-size', nonce, height });
  };
  window.vbot = Object.freeze({
    setState(values) {
      const copy = plain(values);
      if (!copy || typeof copy !== 'object' || Array.isArray(copy)) return;
      Object.assign(custom, copy);
      schedule();
    },
    get state() {
      return plain(collect());
    },
  });
  document.addEventListener('input', schedule, true);
  document.addEventListener('change', schedule, true);
  // Forms cannot submit (form-action 'none'); Enter in a field must not try.
  document.addEventListener('submit', (event) => event.preventDefault(), true);
  window.addEventListener('message', (event) => {
    const data = event.data;
    if (event.source !== window.parent || nonce !== null) return;
    if (!data || data.type !== 'vbot-widget-init' || typeof data.nonce !== 'string') return;
    nonce = data.nonce;
    const state = data.state;
    if (state && typeof state === 'object' && !Array.isArray(state)) restore(state);
    schedule();
    measure();
    if (typeof ResizeObserver === 'function') new ResizeObserver(measure).observe(document.body);
  });
  let hellos = 0;
  const hello = () => {
    if (nonce !== null || hellos >= 20) return;
    hellos += 1;
    post({ type: 'vbot-widget-hello' });
    setTimeout(hello, 500);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', hello);
  else hello();
})();
</script>"""

_DOCUMENT_TAG = re.compile(r"<html[\s>]", re.IGNORECASE)
_HEAD_OPEN = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html(?:\s[^>]*)?>", re.IGNORECASE)
_UNAVAILABLE = (
    '<!doctype html><html><head><meta charset="utf-8"></head>'
    '<body style="margin:0;padding:12px;font:14px system-ui,sans-serif;color:#b3aa9e;'
    'background:transparent">This widget is no longer available.</body></html>'
)


def widget_document(title: str, widget_code: str) -> bytes:
    """Return the complete document for one widget's stored HTML."""
    shell = _BASE_STYLE + _BRIDGE_SCRIPT
    if _DOCUMENT_TAG.search(widget_code) is not None:
        # The Agent wrote a whole document: keep it, adding the shell first.
        for opening in (_HEAD_OPEN, _HTML_OPEN):
            match = opening.search(widget_code)
            if match is not None:
                document = widget_code[: match.end()] + shell + widget_code[match.end() :]
                return document.encode("utf-8")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>{shell}</head><body>{widget_code}</body></html>"
    ).encode()


def unavailable_widget_document() -> bytes:
    """Return the document shown once a widget's Session no longer keeps it."""
    return _UNAVAILABLE.encode("utf-8")
