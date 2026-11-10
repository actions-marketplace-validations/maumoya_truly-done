#!/usr/bin/env python3
"""truly-done: prove a change works in the running web app. Verdict: pass, fail or cannot tell.

A flow file (JSON) lists steps a user would take and claims that must hold afterwards. truly-done drives the
Chrome already installed on this machine (headless, throwaway profile) with real mouse and keyboard input, checks
every claim against the live page and writes a report with the evidence.

What it never does: send usage data, call home, update itself, edit any config file, open a listening port,
run a background process, or read your browser profile. Python standard library only.

    verify.py run FLOW.json [FLOW.json ...] [--url BASE] [--out DIR] [--mine] [--allow-origin HOST] [--offline]
    verify.py gate DIR_OR_REPORT ... [--allow-unknown]
    verify.py check FLOW.json ...
    verify.py snapshot URL [--mine]

Exit codes: 0 pass, 1 fail, 2 cannot tell, 3 refused or bad input.
"""
import argparse
import base64
import ipaddress
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp  # noqa: E402

VERSION = "0.1.0"
PASS, FAIL, UNKNOWN = "pass", "fail", "cannot_tell"
EXIT = {PASS: 0, FAIL: 1, UNKNOWN: 2}
WORST = {PASS: 0, UNKNOWN: 1, FAIL: 2}

MAX_STEPS, MAX_CLAIMS = 200, 100
MAX_WAIT_MS, MAX_TIMEOUT_MS = 10_000, 30_000
DEFAULT_TIMEOUT_MS = 3_000
MAX_RUN_SECONDS = 300
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
QUIET_TYPES = {"EventSource", "WebSocket", "Media", "Ping"}
IGNORED_FAILURES = ("/favicon.ico",)

TARGET_KEYS = {"selector", "testid", "label", "text", "role"}
STEP_KINDS = {"goto", "reload", "click", "type", "press", "select", "check", "hover", "wait", "wait_for", "expect"}
EXPECT_KINDS = {"text", "no_text", "count", "visible", "hidden", "value", "url", "title", "no_console_errors",
                "no_failed_requests", "request", "no_request", "dialog"}
KEYS = {  # name: (windowsVirtualKeyCode, code, text)
    "Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", ""), "Escape": (27, "Escape", ""),
    "Backspace": (8, "Backspace", ""), "Delete": (46, "Delete", ""), "Space": (32, "Space", " "),
    "ArrowUp": (38, "ArrowUp", ""), "ArrowDown": (40, "ArrowDown", ""),
    "ArrowLeft": (37, "ArrowLeft", ""), "ArrowRight": (39, "ArrowRight", ""),
    "Home": (36, "Home", ""), "End": (35, "End", ""), "PageUp": (33, "PageUp", ""), "PageDown": (34, "PageDown", ""),
}


class Refused(Exception):
    """truly-done will not run against this target."""


class FlowError(Exception):
    """The flow file is not valid."""


class Stop(Exception):
    """A step could not be completed; carries the verdict and the reason."""

    def __init__(self, verdict: str, note: str):
        super().__init__(note)
        self.verdict, self.note = verdict, note


# --- policy (pure, unit-tested) ---------------------------------------------------------------------------

def is_loopback(host: str) -> bool:
    host = (host or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def split_url(url: str):
    """urlsplit that answers None for a URL Python cannot parse (bad IPv6 brackets, bad port)."""
    try:
        p = urllib.parse.urlsplit(url)
        p.port  # noqa: B018 - raises ValueError when the port is not a number in range
        return p
    except ValueError:
        return None


_BARE_LOOPBACK = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/.*)?$", re.I)


def with_scheme(url):
    """`localhost:3000` means `http://localhost:3000`. Only loopback hosts get a default scheme."""
    return "http://" + url if isinstance(url, str) and _BARE_LOOPBACK.match(url) else url


def origin_of(url: str) -> str:
    p = split_url(url)
    return f"{p.scheme}://{p.netloc}".lower() if p else ""


def check_target(url: str, mine: bool) -> str:
    """The start URL, normalized. Loopback always; anything else only when the user says the app is theirs."""
    p = split_url(url)
    if p is None or p.scheme not in ("http", "https") or not p.hostname:
        raise Refused(f"not an http(s) URL: {url!r}")
    if not is_loopback(p.hostname) and not mine:
        raise Refused(f"{p.hostname} is not this machine. truly-done drives your own app: pass --mine only if "
                      "you own this app or are authorized to test it.")
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))


def join_url(base: str, path: str) -> str:
    """Join a flow path onto the base URL. A query on the base (?flag=on) is kept unless the path has its own."""
    try:
        joined = urllib.parse.urlsplit(urllib.parse.urljoin(base, path))
        if not joined.query:
            joined = joined._replace(query=urllib.parse.urlsplit(base).query)
        return urllib.parse.urlunsplit(joined)
    except ValueError:
        raise Refused(f"not a usable URL: {path!r}") from None


def decide(url: str, method: str, origin: str, allow=(), offline: bool = False) -> str:
    """'' to let a request through, else the reason it is blocked."""
    p = split_url(url)
    if p is None:
        return "an unreadable URL"
    if p.scheme in ("data", "blob", "about"):
        return ""
    if p.scheme not in ("http", "https"):
        return f"scheme {p.scheme or '?'} is not allowed"
    if origin_of(url) == origin or (p.hostname or "").lower() in allow:
        return ""
    if offline:
        return "another origin (offline mode)"
    if method.upper() not in SAFE_METHODS:
        return "write to another origin"
    return ""


def worst(verdicts) -> str:
    out = PASS
    for v in verdicts:
        if WORST[v] > WORST[out]:
            out = v
    return out


def redact(obj, secrets):
    """Replace every secret value in every string (keys too) of a JSON-able object. The structure is kept."""
    forms = set()
    for s in secrets:
        if s:
            forms |= {s, urllib.parse.quote(s), urllib.parse.quote_plus(s)}
    if not forms:
        return obj
    ordered = sorted(forms, key=len, reverse=True)

    def clean(value):
        if isinstance(value, str):
            for form in ordered:
                value = value.replace(form, "[redacted]")
            return value
        if isinstance(value, dict):
            return {clean(k) if isinstance(k, str) else k: clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and any(f in str(value) for f in ordered):
            return "[redacted]"
        return value

    return clean(obj)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "flow").lower()).strip("-")[:60] or "flow"


# --- flow validation (pure, unit-tested) ------------------------------------------------------------------

def _need(cond, where, msg):
    if not cond:
        raise FlowError(f"{where}: {msg}")


def _regex(value, where):
    _need(isinstance(value, str) and value, where, "needs a regular expression string")
    try:
        re.compile(value)
    except re.error as e:
        raise FlowError(f"{where}: bad regular expression ({e})") from None


def _timeout(item, where):
    t = item.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    _need(isinstance(t, int) and not isinstance(t, bool) and 0 <= t <= MAX_TIMEOUT_MS, where,
          f"timeout_ms must be 0..{MAX_TIMEOUT_MS}")


def check_target_spec(target, where):
    if isinstance(target, str):
        target = {"text": target}
    _need(isinstance(target, dict), where, "target must be an object like {\"text\": \"Save\"}")
    kinds = [k for k in TARGET_KEYS if k in target]
    _need(kinds, where, "target needs one of selector, testid, label, text, role")
    main = [k for k in kinds if k != "text"] or ["text"]
    _need(len(main) == 1, where, "target takes one of selector, testid, label, role (role may add text)")
    _need(main[0] in ("role", "text") or "text" not in target, where, "text only combines with role")
    for k in kinds:
        _need(isinstance(target[k], str) and target[k].strip(), where, f"target.{k} must be a non-empty string")
    extra = set(target) - TARGET_KEYS - {"nth"}
    _need(not extra, where, f"unknown target key {sorted(extra)[:1]}")
    if "nth" in target:
        _need(isinstance(target["nth"], int) and not isinstance(target["nth"], bool) and target["nth"] >= 0, where,
              "target.nth must be 0 or more")
    return target


def check_claim(claim, where):
    _need(isinstance(claim, dict), where, "must be an object")
    kind = claim.get("expect")
    _need(kind in EXPECT_KINDS, where, f"expect must be one of {', '.join(sorted(EXPECT_KINDS))}")
    _timeout(claim, where)
    if kind in ("text", "no_text", "dialog"):
        _need(isinstance(claim.get("value"), str) and claim["value"], where, "needs value (text)")
        if "within" in claim:
            _need(isinstance(claim["within"], str) and claim["within"], where, "within must be a CSS selector")
    elif kind == "count":
        _need(isinstance(claim.get("selector"), str) and claim["selector"], where, "needs selector")
        bounds = [k for k in ("equals", "at_least", "at_most") if k in claim]
        _need(bounds, where, "needs equals, at_least or at_most")
        for k in bounds:
            _need(isinstance(claim[k], int) and not isinstance(claim[k], bool) and claim[k] >= 0, where,
                  f"{k} must be 0 or more")
    elif kind in ("visible", "hidden"):
        claim["target"] = check_target_spec(claim.get("target"), where)
    elif kind == "value":
        claim["target"] = check_target_spec(claim.get("target"), where)
        _need(("equals" in claim) != ("checked" in claim), where, "needs equals (text) or checked (true/false)")
        if "equals" in claim:
            _need(isinstance(claim["equals"], str), where, "equals must be text")
        else:
            _need(isinstance(claim["checked"], bool), where, "checked must be true or false")
    elif kind in ("url", "title"):
        _need(("matches" in claim) != ("equals" in claim), where, "needs matches (regex) or equals")
        if "matches" in claim:
            _regex(claim["matches"], where)
        else:
            _need(isinstance(claim["equals"], str), where, "equals must be text")
    elif kind in ("request", "no_request"):
        _regex(claim.get("url_matches"), where)
        if "method" in claim:
            _need(isinstance(claim["method"], str) and claim["method"].isalpha(), where, "method must be a verb")
        if kind == "request":
            for k in ("status", "times"):
                if k in claim:
                    _need(isinstance(claim[k], int) and not isinstance(claim[k], bool) and claim[k] >= 0, where,
                          f"{k} must be a number")
    else:  # no_console_errors, no_failed_requests
        ignore = claim.get("ignore", [])
        _need(isinstance(ignore, list), where, "ignore must be a list of regular expressions")
        for i, pattern in enumerate(ignore):
            _regex(pattern, f"{where}.ignore[{i}]")
    if "claim" in claim:
        _need(isinstance(claim["claim"], str), where, "claim must be text")
    return claim


def check_step(step, where):
    _need(isinstance(step, dict), where, "must be an object")
    kind = step.get("do")
    _need(kind in STEP_KINDS, where, f"do must be one of {', '.join(sorted(STEP_KINDS))}")
    _timeout(step, where)
    if kind in ("click", "hover", "type", "select", "check"):
        step["target"] = check_target_spec(step.get("target"), where)
    if kind == "goto":
        _need(isinstance(step.get("url"), str) and step["url"], where, "needs url")
    elif kind == "type":
        text = step.get("text")
        if isinstance(text, dict):
            _need(set(text) == {"env"} and isinstance(text["env"], str) and re.fullmatch(r"[A-Za-z_]\w*", text["env"]),
                  where, 'a secret is written {"env": "NAME"}')
        else:
            _need(isinstance(text, str), where, 'needs text (a string, or {"env": "NAME"} for a secret)')
    elif kind == "press":
        _need(step.get("key") in KEYS, where, f"key must be one of {', '.join(KEYS)}")
    elif kind == "select":
        _need(isinstance(step.get("option"), str) and step["option"], where, "needs option (its label or value)")
    elif kind == "check":
        _need(isinstance(step.get("checked", True), bool), where, "checked must be true or false")
    elif kind == "wait":
        ms = step.get("ms")
        _need(isinstance(ms, int) and not isinstance(ms, bool) and 0 <= ms <= MAX_WAIT_MS, where,
              f"ms must be 0..{MAX_WAIT_MS}")
    elif kind == "wait_for":
        _need(("text" in step) != ("target" in step), where, "needs text or target")
        if "target" in step:
            step["target"] = check_target_spec(step["target"], where)
        else:
            _need(isinstance(step["text"], str) and step["text"], where, "text must be a non-empty string")
    elif kind == "expect":
        check_claim(step, where)
    return step


def validate_flow(flow):
    """Check a parsed flow file and return it with shorthand filled in. Raises FlowError naming the bad field."""
    _need(isinstance(flow, dict), "flow", "must be a JSON object")
    _need(isinstance(flow.get("name"), str) and flow["name"].strip(), "flow.name", "is required")
    _need(isinstance(flow.get("url"), str) and flow["url"], "flow.url", "is required (a full URL or a path)")
    steps, claims = flow.get("steps", []), flow.get("claims", [])
    _need(isinstance(steps, list) and len(steps) <= MAX_STEPS, "flow.steps", f"must be a list of at most {MAX_STEPS}")
    _need(isinstance(claims, list) and len(claims) <= MAX_CLAIMS, "flow.claims",
          f"must be a list of at most {MAX_CLAIMS}")
    flow["steps"] = [check_step(s, f"steps[{i}]") for i, s in enumerate(steps)]
    flow["claims"] = [check_claim(c, f"claims[{i}]") for i, c in enumerate(claims)]
    _need(flow.get("dialogs", "accept") in ("accept", "dismiss"), "flow.dialogs", "must be accept or dismiss")
    view = flow.get("viewport")
    if view is not None:
        _need(isinstance(view, dict) and all(isinstance(view.get(k), int) and 200 <= view[k] <= 4000
                                             for k in ("width", "height")), "flow.viewport",
              "needs width and height between 200 and 4000")
    extra = set(flow) - {"name", "url", "steps", "claims", "dialogs", "viewport", "screenshot", "notes"}
    _need(not extra, "flow", f"unknown key {sorted(extra)[:1]}")
    return flow


def load_flow(path: str):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FlowError(f"{path}: cannot read as JSON ({e})") from None
    return validate_flow(data)


# --- page scripts -----------------------------------------------------------------------------------------

FIND_JS = r"""
(function (t) {
  const norm = s => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim().toLowerCase();
  const shown = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0; };
  const labelsOf = el => (el.labels ? Array.from(el.labels).map(l => l.innerText) : []);
  const names = el => [el.getAttribute('aria-label'), ...labelsOf(el), el.innerText, el.value,
    el.getAttribute('title'), el.getAttribute('alt'), el.getAttribute('placeholder')].map(norm).filter(Boolean);
  const roleOf = el => { const r = el.getAttribute('role'); if (r) return r.toLowerCase();
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'input') return ({checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button',
      reset: 'button', range: 'slider'})[type] || 'textbox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    return ({img: 'img', li: 'listitem', nav: 'navigation', main: 'main', form: 'form', dialog: 'dialog',
      table: 'table', option: 'option'})[tag] || ''; };
  const desc = el => { if (!el) return ''; const tag = el.tagName.toLowerCase();
    return '<' + tag + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' +
      el.className.trim().split(/\s+/).slice(0, 2).join('.') : '') + '> ' + norm(el.innerText || el.value || '').slice(0, 60); };
  const byName = (els, want) => { const exact = els.filter(el => names(el).includes(want));
    return exact.length ? exact : els.filter(el => names(el).some(n => n.includes(want))); };
  let els = [];
  try {
    if (t.selector) els = Array.from(document.querySelectorAll(t.selector));
    else if (t.testid) els = Array.from(document.querySelectorAll('[data-testid]')).filter(el => el.getAttribute('data-testid') === t.testid);
    else if (t.label) els = byName(Array.from(document.querySelectorAll('input,textarea,select,[contenteditable=""],[contenteditable="true"]')), norm(t.label));
    else if (t.role) { els = Array.from(document.querySelectorAll('*')).filter(el => roleOf(el) === t.role.toLowerCase());
      if (t.text) els = byName(els, norm(t.text)); }
    else { const want = norm(t.text);
      els = byName(Array.from(document.querySelectorAll('button,a,[role],input[type=submit],input[type=button],summary,label,option')), want);
      if (!els.length) { const all = Array.from(document.querySelectorAll('body *')).slice(0, 8000);
        let hit = all.filter(el => norm(el.innerText) === want);
        if (!hit.length) hit = all.filter(el => norm(el.innerText).includes(want));
        els = hit.filter(el => !hit.some(o => o !== el && el.contains(o))); } }
  } catch (e) { return {status: 'invalid', note: String(e && e.message || e)}; }
  const total = els.length;
  let vis = els.filter(shown);
  if (typeof t.nth === 'number') vis = vis[t.nth] ? [vis[t.nth]] : [];
  if (!vis.length) return {status: total ? 'hidden' : 'missing', total: total};
  if (vis.length > 1) return {status: 'ambiguous', total: vis.length, matches: vis.slice(0, 5).map(desc)};
  const el = vis[0];
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
  const top = document.elementFromPoint(x, y);
  const lab = top && top.closest ? top.closest('label') : null;
  const free = !!top && (top === el || el.contains(top) || (lab && el.labels && Array.from(el.labels).includes(lab)));
  window.__truly_done_el = el;
  return {status: 'ok', x: x, y: y, covered: !free, coveredBy: free ? '' : desc(top),
    enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true', tag: el.tagName.toLowerCase(),
    checked: !!el.checked, value: el.value == null ? norm(el.innerText) : String(el.value), desc: desc(el)};
})
"""

TEXT_JS = r"""
(function (within) {
  try { const els = within ? Array.from(document.querySelectorAll(within)) : [document.body];
    return {ok: true, found: els.length, text: els.map(el => el ? el.innerText : '').join('\n')};
  } catch (e) { return {ok: false, note: String(e && e.message || e)}; }
})
"""

COUNT_JS = r"""
(function (selector, hidden) {
  try { const els = Array.from(document.querySelectorAll(selector));
    return {ok: true, n: hidden ? els.length : els.filter(el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; }).length};
  } catch (e) { return {ok: false, note: String(e && e.message || e)}; }
})
"""

SELECT_JS = r"""
(function (option) {
  const el = window.__truly_done_el, norm = s => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  if (!el || el.tagName !== 'SELECT') return 'not a select';
  const hit = Array.from(el.options).filter(o => o.value === option || norm(o.label || o.text) === norm(option));
  if (hit.length !== 1) return hit.length ? 'more than one option matches' : 'no such option';
  if (hit[0].disabled) return 'option is disabled';
  el.value = hit[0].value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return '';
})
"""

SNAPSHOT_JS = r"""
(function () {
  const norm = s => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
  const shown = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const pick = el => { const tag = el.tagName.toLowerCase(), o = {tag: tag};
    const label = el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('placeholder');
    if (label) o.label = norm(label).slice(0, 80);
    const text = norm(el.innerText || (tag === 'input' && /submit|button/.test(el.type) ? el.value : '')).slice(0, 80);
    if (text) o.text = text;
    if (el.getAttribute('data-testid')) o.testid = el.getAttribute('data-testid');
    if (el.id) o.selector = '#' + CSS.escape(el.id);
    if (el.getAttribute('role')) o.role = el.getAttribute('role');
    if (el.type && tag === 'input') o.type = el.type;
    if (el.disabled) o.disabled = true;
    return o; };
  const q = s => Array.from(document.querySelectorAll(s)).filter(shown);
  return {url: location.href, title: document.title,
    headings: q('h1,h2,h3').slice(0, 30).map(el => norm(el.innerText).slice(0, 100)),
    controls: q('button,a[href],input,textarea,select,summary,[role=button],[role=link],[role=tab],[role=checkbox],[role=menuitem],[contenteditable=""],[contenteditable="true"]').slice(0, 200).map(pick)};
})
"""


def js(fn: str, *args) -> str:
    return f"({fn})({', '.join(json.dumps(a) for a in args)})"


# --- the runner -------------------------------------------------------------------------------------------

class Runner:
    def __init__(self, flow: dict, start_url: str, allow=(), offline=False, out: Path = None, chrome: str = None):
        self.flow, self.start_url, self.origin = flow, start_url, origin_of(start_url)
        self.allow, self.offline, self.out = tuple(h.lower() for h in allow), offline, out
        self.chrome = chrome or cdp.find_chrome()
        self.requests, self.by_id, self.inflight = [], {}, set()
        self.console, self.blocked, self.dialogs, self.sockets = [], [], [], []
        self.loaded, self.crashed, self.secrets = False, False, []
        self.browser = None

    # CDP events
    def on_event(self, method: str, params: dict):
        if method == "Fetch.requestPaused":
            req = params.get("request", {})
            reason = decide(req.get("url", ""), req.get("method", "GET"), self.origin, self.allow, self.offline)
            if reason:
                self.blocked.append({"method": req.get("method", "GET"), "url": req.get("url", ""), "reason": reason})
                self.browser.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"})
            else:
                self.browser.send("Fetch.continueRequest", {"requestId": params["requestId"]})
        elif method == "Network.requestWillBeSent":
            rid, req = params.get("requestId"), params.get("request", {})
            old = self.by_id.get(rid)
            if old is not None and "redirectResponse" in params:
                old["status"] = params["redirectResponse"].get("status")
            entry = {"method": req.get("method", "GET"), "url": req.get("url", ""), "type": params.get("type", ""),
                     "status": None, "error": ""}
            self.by_id[rid] = entry
            self.requests.append(entry)
            if params.get("type") not in QUIET_TYPES:
                self.inflight.add(rid)
        elif method == "Network.responseReceived":
            entry = self.by_id.get(params.get("requestId"))
            if entry is not None:
                entry["status"] = params.get("response", {}).get("status")
        elif method == "Network.loadingFinished":
            self.inflight.discard(params.get("requestId"))
        elif method == "Network.loadingFailed":
            self.inflight.discard(params.get("requestId"))
            entry = self.by_id.get(params.get("requestId"))
            if entry is not None and not params.get("canceled"):
                entry["error"] = params.get("errorText", "failed")
        elif method == "Network.webSocketCreated":
            url = params.get("url", "")
            if origin_of(url.replace("ws", "http", 1)) != self.origin:
                self.sockets.append(url)
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            parts = [str(a.get("value", a.get("description", ""))) for a in params.get("args", [])]
            self.console.append(" ".join(parts)[:500])
        elif method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails", {})
            self.console.append(("Uncaught: " + str(d.get("exception", {}).get("description") or d.get("text", "")))[:500])
        elif method == "Log.entryAdded":
            e = params.get("entry", {})
            if e.get("level") == "error" and e.get("source") != "network":
                self.console.append(str(e.get("text", ""))[:500])
        elif method == "Page.loadEventFired":
            self.loaded = True
        elif method == "Page.javascriptDialogOpening":
            self.dialogs.append({"type": params.get("type", ""), "message": params.get("message", "")[:300]})
            self.browser.send("Page.handleJavaScriptDialog", {"accept": self.flow.get("dialogs", "accept") == "accept"})
        elif method == "Inspector.targetCrashed":
            self.crashed = True

    # waiting
    def settle(self, max_wait=5.0, quiet=0.35):
        deadline, quiet_since = time.monotonic() + max_wait, None
        while time.monotonic() < deadline:
            self.browser.pump(0.05)
            if self.inflight:
                quiet_since = None
                continue
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= quiet:
                return

    def evaluate(self, expression: str):
        if self.crashed:
            raise Stop(UNKNOWN, "the page crashed")
        try:
            return self.browser.evaluate(expression, timeout=10)
        except cdp.CdpError as e:
            raise Stop(UNKNOWN, f"could not read the page ({e})") from None

    def poll(self, fn, timeout_ms: int):
        """Call fn() until it returns a truthy first item or time runs out; returns the last result."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            result = fn()
            if result[0] or time.monotonic() >= deadline:
                return result
            self.browser.pump(0.1)

    def navigate(self, url: str, what: str):
        reason = decide(url, "GET", self.origin, self.allow, self.offline)
        if not reason and origin_of(url) != self.origin and (split_url(url).hostname or "").lower() not in self.allow:
            reason = "another origin"  # a flow may read third-party assets, never walk to another site
        if reason:
            raise Stop(UNKNOWN, f"{what} refused: {url} is {reason}; truly-done stays on {self.origin}")
        self.loaded, self.inflight = False, set()
        mark = len(self.requests)
        res = self.browser.call("Page.navigate", {"url": url})
        if res.get("errorText"):
            raise Stop(UNKNOWN, f"{what}: the app did not answer at {url} ({res['errorText']}). Is it running?")
        self.wait_load()
        doc = next((r for r in self.requests[mark:] if r["type"] == "Document"), None)
        if doc and (doc["status"] or 0) >= 400:
            raise Stop(FAIL, f"{what}: {url} answered HTTP {doc['status']}")

    def wait_load(self):
        deadline = time.monotonic() + 20
        while not self.loaded and time.monotonic() < deadline:
            self.browser.pump(0.1)
        self.settle()

    # targets
    def find(self, target: dict) -> dict:
        return self.evaluate(js(FIND_JS, target)) or {"status": "invalid", "note": "no answer from the page"}

    def locate(self, step: dict, usable=True) -> dict:
        """The one visible element a step names; waits up to the step timeout for it to be usable."""
        target = step["target"]

        def attempt():
            f = self.find(target)
            return (f["status"] == "ok" and (not usable or (f["enabled"] and not f["covered"])), f)

        _, f = self.poll(attempt, step.get("timeout_ms", DEFAULT_TIMEOUT_MS))
        name = json.dumps(target)
        if f["status"] == "invalid":
            raise Stop(UNKNOWN, f"target {name} is not valid: {f.get('note', '')}")
        if f["status"] == "missing":
            raise Stop(UNKNOWN, f"nothing on the page matches {name}")
        if f["status"] == "ambiguous":
            raise Stop(UNKNOWN, f"{f['total']} elements match {name}: {'; '.join(f['matches'])}. Narrow the target.")
        if f["status"] == "hidden":
            raise Stop(FAIL, f"{name} exists but is not visible, so a user cannot use it")
        if usable and not f["enabled"]:
            raise Stop(FAIL, f"{f['desc']} is disabled")
        if usable and f["covered"]:
            raise Stop(FAIL, f"{f['desc']} is covered by {f['coveredBy']}, so a real click cannot reach it")
        return f

    def mouse(self, kind: str, f: dict, **more):
        self.browser.call("Input.dispatchMouseEvent", {"type": kind, "x": f["x"], "y": f["y"], **more})

    def click(self, f: dict):
        self.mouse("mouseMoved", f)
        self.mouse("mousePressed", f, button="left", clickCount=1)
        self.mouse("mouseReleased", f, button="left", clickCount=1)

    def key(self, name: str):
        code, key_code, text = KEYS[name]
        base = {"key": " " if name == "Space" else name, "code": key_code, "windowsVirtualKeyCode": code}
        self.browser.call("Input.dispatchKeyEvent", {"type": "keyDown" if text else "rawKeyDown", "text": text, **base})
        self.browser.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    # steps
    def do_step(self, step: dict) -> str:
        kind = step["do"]
        if kind == "goto":
            try:
                url = join_url(self.start_url, step["url"])
            except Refused as e:
                raise Stop(UNKNOWN, f"goto refused: {e}") from None
            self.navigate(url, "goto")
        elif kind == "reload":
            self.loaded, self.inflight = False, set()
            self.browser.call("Page.reload")
            self.wait_load()
        elif kind == "wait":
            self.browser.pump(step["ms"] / 1000)
        elif kind == "press":
            self.key(step["key"])
        elif kind == "hover":
            self.mouse("mouseMoved", self.locate(step, usable=False))
        elif kind == "click":
            self.click(self.locate(step))
        elif kind == "type":
            f = self.locate(step)
            self.click(f)
            if not self.evaluate("(()=>{const e=window.__truly_done_el,a=document.activeElement;"
                                 "return !!e&&(e===a||e.contains(a))})()"):
                raise Stop(FAIL, f"clicking {f['desc']} did not put the cursor in it")
            text = step["text"]
            if isinstance(text, dict):
                value = os.environ.get(text["env"])
                if value is None:
                    raise Stop(UNKNOWN, f"environment variable {text['env']} is not set")
                self.secrets.append(value)
                text = value
            if step.get("clear", True):
                self.evaluate("(()=>{const a=document.activeElement;if(a&&a.select)a.select();"
                              "else document.execCommand('selectAll')})()")
                if not text:
                    self.key("Delete")
            if text:
                self.browser.call("Input.insertText", {"text": text})
        elif kind == "select":
            self.locate(step, usable=False)
            problem = self.evaluate(js(SELECT_JS, step["option"]))
            if problem:
                raise Stop(UNKNOWN if problem.startswith(("no such", "more than", "not a")) else FAIL,
                           f"select {step['option']!r}: {problem}")
            self.settle()
            return "option set by script (a native dropdown cannot be opened headless)"
        elif kind == "check":
            f = self.locate(step)
            if f["checked"] != step.get("checked", True):
                self.click(f)
        elif kind == "wait_for":
            if "text" in step:
                want = " ".join(step["text"].split()).lower()

                def seen():
                    return (want in " ".join(str(self.evaluate(js(TEXT_JS, None)).get("text", "")).split()).lower(), 0)
            else:
                def seen():
                    return (self.find(step["target"])["status"] in ("ok", "ambiguous"), 0)
            if not self.poll(seen, step.get("timeout_ms", DEFAULT_TIMEOUT_MS))[0]:
                raise Stop(FAIL, f"waited {step.get('timeout_ms', DEFAULT_TIMEOUT_MS)} ms and it never appeared")
        if kind not in ("wait", "wait_for"):
            self.settle()
        return ""

    # claims
    def failed_requests(self, ignore=()):
        out = []
        for r in self.requests:
            bad = (r["status"] or 0) >= 400 or (r["error"] and "BLOCKED_BY_CLIENT" not in r["error"])
            if bad and not r["url"].split("?")[0].endswith(IGNORED_FAILURES) and not any(re.search(p, r["url"]) for p in ignore):
                out.append(f"{r['method']} {r['url']} -> {r['status'] or r['error']}")
        return out

    def eval_claim(self, c: dict):
        """(verdict, evidence) for one claim, read from the page as it is now."""
        kind = c["expect"]
        if kind in ("text", "no_text"):
            got = self.evaluate(js(TEXT_JS, c.get("within")))
            if not got.get("ok"):
                return UNKNOWN, f"within selector is not valid: {got.get('note', '')}"
            if c.get("within") and not got["found"]:
                return (PASS, f"nothing matches {c['within']}") if kind == "no_text" else \
                    (FAIL, f"nothing on the page matches {c['within']}")
            hay, want = " ".join(got["text"].split()), " ".join(c["value"].split())
            if not c.get("exact"):
                hay, want = hay.lower(), want.lower()
            there = want in hay
            if kind == "text":
                return (PASS, "text is on the page") if there else (FAIL, f"text {c['value']!r} is not on the page")
            return (FAIL, f"text {c['value']!r} is on the page") if there else (PASS, "text is not on the page")
        if kind == "count":
            got = self.evaluate(js(COUNT_JS, c["selector"], bool(c.get("include_hidden"))))
            if not got.get("ok"):
                return UNKNOWN, f"selector is not valid: {got.get('note', '')}"
            n = got["n"]
            ok = all([c.get("equals", n) == n, n >= c.get("at_least", n), n <= c.get("at_most", n)])
            return (PASS if ok else FAIL), f"{n} visible element(s) match {c['selector']}"
        if kind in ("visible", "hidden"):
            f = self.find(c["target"])
            if f["status"] == "invalid":
                return UNKNOWN, f"target is not valid: {f.get('note', '')}"
            seen = f["status"] in ("ok", "ambiguous")
            return (PASS if seen == (kind == "visible") else FAIL), ("it is visible" if seen else "it is not visible")
        if kind == "value":
            f = self.find(c["target"])
            if f["status"] != "ok":
                return UNKNOWN, {"invalid": "target is not valid", "missing": "nothing matches the target",
                                 "hidden": "the target is not visible",
                                 "ambiguous": "more than one element matches the target"}[f["status"]]
            if "checked" in c:
                return (PASS if f["checked"] == c["checked"] else FAIL), f"checked is {str(f['checked']).lower()}"
            return (PASS if f["value"] == c["equals"] else FAIL), f"value is {f['value']!r}"
        if kind in ("url", "title"):
            got = self.evaluate("location.href" if kind == "url" else "document.title") or ""
            ok = re.search(c["matches"], got) is not None if "matches" in c else got == c["equals"]
            return (PASS if ok else FAIL), f"{kind} is {got!r}"
        if kind == "no_console_errors":
            errs = [e for e in self.console if not any(re.search(p, e) for p in c.get("ignore", []))]
            return (FAIL, f"{len(errs)} console error(s): " + " | ".join(errs[:3])) if errs else (PASS, "no console errors")
        if kind == "no_failed_requests":
            bad = self.failed_requests(c.get("ignore", []))
            return (FAIL, f"{len(bad)} failed request(s): " + " | ".join(bad[:3])) if bad else (PASS, "no failed requests")
        if kind in ("request", "no_request"):
            verb = c.get("method", "").upper()
            hits = [r for r in self.requests if re.search(c["url_matches"], r["url"]) and (not verb or r["method"] == verb)]
            what = f"{verb or 'any'} {c['url_matches']}"
            if kind == "no_request":
                return (FAIL, f"{len(hits)} request(s) matched {what}") if hits else (PASS, f"no request matched {what}")
            if not hits:
                return FAIL, f"no request matched {what}"
            if "status" in c:
                if any(r["status"] is None and not r["error"] for r in hits):
                    return FAIL, f"{what} has not been answered yet"
                good = [r for r in hits if r["status"] == c["status"]]
                if not good:
                    return FAIL, f"{what} answered {', '.join(str(r['status'] or r['error']) for r in hits[:3])}, not {c['status']}"
                hits = good
            if "times" in c and len(hits) != c["times"]:
                return FAIL, f"{what} was sent {len(hits)} time(s), not {c['times']}"
            return PASS, f"{what} sent {len(hits)} time(s)" + (f", answered {c['status']}" if "status" in c else "")
        want = c["value"].lower()   # dialog
        hit = [d for d in self.dialogs if want in d["message"].lower()]
        return (PASS, f"dialog shown: {hit[0]['message']!r}") if hit else (FAIL, f"no dialog said {c['value']!r}")

    def check(self, c: dict) -> dict:
        self.settle(max_wait=2.0)
        try:
            verdict, evidence = self.poll(lambda: _pass_first(self.eval_claim(c)), c.get("timeout_ms", DEFAULT_TIMEOUT_MS))[1]
        except Stop as s:
            verdict, evidence = s.verdict, s.note
        return {"claim": c.get("claim") or describe(c), "expect": c["expect"], "verdict": verdict, "evidence": evidence}

    # the run
    def run(self) -> dict:
        started, t0 = time.strftime("%Y-%m-%dT%H:%M:%S%z"), time.monotonic()
        steps, claims, stopped = [], [], ""
        if not self.chrome:
            return self.report(started, 0, steps, claims, "Chrome (or Chromium) is not installed; truly-done never downloads one")
        extra = ["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"] \
            if self.offline and is_loopback(split_url(self.start_url).hostname) else []
        try:
            self.browser = cdp.Browser(self.chrome, self.on_event, extra_args=extra)
        except (cdp.CdpError, OSError) as e:
            return self.report(started, 0, steps, claims, f"could not start Chrome ({e})")
        shot = ""
        try:
            b = self.browser
            for domain in ("Page", "Runtime", "Network", "Log"):
                b.call(domain + ".enable")
            b.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
            b.call("Browser.setDownloadBehavior", {"behavior": "deny"}, browser_level=True)
            view = self.flow.get("viewport")
            if view:
                b.call("Emulation.setDeviceMetricsOverride", {"width": view["width"], "height": view["height"],
                                                              "deviceScaleFactor": 1, "mobile": bool(view.get("mobile"))})
            plan = [{"do": "goto", "url": self.start_url}] + self.flow["steps"]
            for i, step in enumerate(plan):
                row = {"n": i, "do": step["do"], "verdict": PASS, "note": ""}
                if "target" in step and step["do"] != "expect":
                    row["target"] = step["target"]
                if time.monotonic() - t0 > MAX_RUN_SECONDS:
                    row.update(verdict=UNKNOWN, note=f"run passed {MAX_RUN_SECONDS} s; stopped")
                elif step["do"] == "expect":
                    result = self.check(step)
                    claims.append(result)
                    row.update(verdict=PASS, note=f"checked: {result['verdict']}")
                else:
                    try:
                        row["note"] = self.do_step(step)
                    except Stop as s:
                        row.update(verdict=s.verdict, note=s.note)
                    except cdp.CdpError as e:
                        row.update(verdict=UNKNOWN, note=f"lost the browser ({e})")
                steps.append(row)
                if row["verdict"] != PASS:
                    stopped = f"step {i} ({step['do']}) {'failed' if row['verdict'] == FAIL else 'could not run'}: {row['note']}"
                    break
            for c in self.flow["claims"]:
                if stopped:
                    claims.append({"claim": c.get("claim") or describe(c), "expect": c["expect"], "verdict": UNKNOWN,
                                   "evidence": "not checked: the flow stopped early"})
                else:
                    claims.append(self.check(c))
            if self.out and self.flow.get("screenshot", True):
                try:
                    data = b.call("Page.captureScreenshot", {"format": "png"}, timeout=10).get("data", "")
                    self.out.mkdir(parents=True, exist_ok=True)
                    (self.out / "final.png").write_bytes(base64.b64decode(data))
                    shot = "final.png"
                except (cdp.CdpError, OSError, ValueError):
                    shot = ""
        except cdp.CdpError as e:
            stopped = stopped or f"lost the browser ({e})"
            steps.append({"n": len(steps), "do": "browser", "verdict": UNKNOWN, "note": stopped})
        finally:
            self.browser.close()
        return self.report(started, time.monotonic() - t0, steps, claims, stopped, shot)

    def report(self, started, seconds, steps, claims, stopped="", shot="") -> dict:
        verdicts = [s["verdict"] for s in steps] + [c["verdict"] for c in claims]
        if not steps:
            verdict, why = UNKNOWN, stopped
        elif not claims and worst(verdicts) == PASS:
            verdict, why = UNKNOWN, "the flow has no claims, so nothing was verified"
        else:
            verdict = worst(verdicts)
            why = stopped or next((f"{c['claim']}: {c['evidence']}" for c in claims if c["verdict"] == verdict != PASS), "")
        rep = {"tool": "truly-done", "version": VERSION, "flow": self.flow["name"], "url": self.start_url,
               "verdict": verdict, "why": why, "started": started, "seconds": round(seconds, 1),
               "steps": steps, "claims": claims, "console_errors": self.console[:50],
               "failed_requests": self.failed_requests()[:50], "blocked_requests": self.blocked[:50],
               "websockets_to_other_origins": self.sockets[:20], "dialogs": self.dialogs[:20],
               "requests": len(self.requests), "screenshot": shot}
        return redact(rep, self.secrets)


def _pass_first(result):
    return (result[0] == PASS, result)


def describe(c: dict) -> str:
    kind = c["expect"]
    if kind in ("text", "no_text", "dialog"):
        return f"{kind.replace('_', ' ')} {c['value']!r}" + (f" within {c['within']}" if c.get("within") else "")
    if kind == "count":
        return f"count of {c['selector']} " + ", ".join(f"{k.replace('_', ' ')} {c[k]}" for k in ("equals", "at_least", "at_most") if k in c)
    if kind in ("visible", "hidden", "value"):
        tail = f" is {c['equals']!r}" if "equals" in c else (f" checked is {str(c['checked']).lower()}" if "checked" in c else "")
        return f"{kind} {json.dumps(c['target'])}{tail}"
    if kind in ("url", "title"):
        return f"{kind} {'matches ' + c['matches'] if 'matches' in c else 'equals ' + repr(c['equals'])}"
    if kind in ("request", "no_request"):
        return f"{kind.replace('_', ' ')} {c.get('method', 'any').upper()} {c['url_matches']}" + (f" -> {c['status']}" if "status" in c else "")
    return kind.replace("_", " ")


# --- reports and commands ---------------------------------------------------------------------------------

MARK = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "CANNOT TELL"}


def to_markdown(rep: dict) -> str:
    lines = [f"# truly-done: {rep['flow']}", "",
             f"**Verdict: {MARK[rep['verdict']]}**" + (f" ({rep['why']})" if rep["why"] else ""), "",
             f"- App: {rep['url']}", f"- When: {rep['started']}, {rep['seconds']} s, {rep['requests']} requests", ""]
    if rep["claims"]:
        lines += ["| Claim | Verdict | Evidence |", "|---|---|---|"]
        lines += [f"| {_cell(c['claim'])} | {MARK[c['verdict']]} | {_cell(c['evidence'])} |" for c in rep["claims"]]
        lines.append("")
    lines += ["| Step | Did | Result |", "|---|---|---|"]
    for s in rep["steps"]:
        did = s["do"] + (" " + json.dumps(s["target"]) if "target" in s else "")
        lines.append(f"| {s['n']} | {_cell(did)} | {MARK[s['verdict']] if s['verdict'] != PASS else 'ok'}"
                     f"{': ' + _cell(s['note']) if s['note'] else ''} |")
    for title, key in (("Console errors", "console_errors"), ("Failed requests", "failed_requests")):
        if rep[key]:
            lines += ["", f"## {title}", ""] + [f"- `{_cell(str(x))}`" for x in rep[key][:20]]
    if rep["blocked_requests"]:
        lines += ["", "## Blocked by truly-done (never sent)", ""]
        lines += [f"- `{b['method']} {_cell(b['url'])}`: {b['reason']}" for b in rep["blocked_requests"][:20]]
    if rep["websockets_to_other_origins"]:
        lines += ["", "## WebSockets to other origins (not filtered)", ""]
        lines += [f"- `{_cell(u)}`" for u in rep["websockets_to_other_origins"]]
    if rep["screenshot"]:
        lines += ["", f"![final state]({rep['screenshot']})"]
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")[:300]


def cmd_run(args) -> int:
    results = []
    for path in args.flows:
        flow = load_flow(path)
        base = with_scheme(args.url or flow["url"])
        start = check_target(join_url(base, flow["url"]) if args.url else base, args.mine)
        out = Path(args.out) / slug(flow["name"])
        rep = Runner(flow, start, allow=args.allow_origin, offline=args.offline, out=out).run()
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
        (out / "report.md").write_text(to_markdown(rep), encoding="utf-8")
        for c in rep["claims"]:
            print(f"  {MARK[c['verdict']]:<11} {c['claim']}" + ("" if c["verdict"] == PASS else f"  <- {c['evidence']}"))
        print(f"VERDICT {MARK[rep['verdict']]}  {rep['flow']}  ({len(rep['claims'])} claims, {rep['seconds']} s)"
              + (f"  {rep['why']}" if rep["why"] else ""))
        print(f"report: {out / 'report.md'}")
        results.append(rep["verdict"])
    return EXIT[worst(results)] if results else 3


def cmd_gate(args) -> int:
    files = []
    for item in args.paths:
        p = Path(item)
        files += sorted(p.rglob("report.json")) if p.is_dir() else [p]
    verdicts = []
    for f in files:
        try:
            rep = json.loads(f.read_text(encoding="utf-8"))
            verdict = rep["verdict"] if rep.get("tool") == "truly-done" and rep.get("verdict") in EXIT else UNKNOWN
            name = str(rep.get("flow", f))
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            verdict, name = UNKNOWN, f"{f} (unreadable)"
        print(f"  {MARK[verdict]:<11} {name}")
        verdicts.append(verdict)
    if not verdicts:
        print("GATE CANNOT TELL: no truly-done report found")
        return 2
    total = worst(PASS if v == UNKNOWN and args.allow_unknown else v for v in verdicts)
    print(f"GATE {MARK[total]}: {verdicts.count(PASS)} pass, {verdicts.count(FAIL)} fail, "
          f"{verdicts.count(UNKNOWN)} cannot tell")
    return EXIT[total]


def cmd_check(args) -> int:
    for path in args.flows:
        flow = load_flow(path)
        print(f"OK {path}: {len(flow['steps'])} steps, "
              f"{len(flow['claims']) + sum(1 for s in flow['steps'] if s['do'] == 'expect')} claims")
    return 0


def cmd_snapshot(args) -> int:
    start = check_target(with_scheme(args.url), args.mine)
    runner = Runner({"name": "snapshot", "url": start, "steps": [], "claims": []}, start)
    if not runner.chrome:
        raise Refused("Chrome (or Chromium) is not installed; truly-done never downloads one")
    runner.browser = cdp.Browser(runner.chrome, runner.on_event)
    try:
        for domain in ("Page", "Runtime", "Network"):
            runner.browser.call(domain + ".enable")
        runner.browser.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        try:
            runner.navigate(start, "snapshot")
            print(json.dumps(runner.evaluate(js(SNAPSHOT_JS)), indent=2))
        except Stop as s:
            print(f"CANNOT TELL: {s.note}")
            return EXIT[UNKNOWN]
    finally:
        runner.browser.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="truly-done", description="Prove a change works in the running app: pass, fail or cannot tell.")
    ap.add_argument("--version", action="version", version=f"truly-done {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run flow files against the running app")
    run.add_argument("flows", nargs="+")
    run.add_argument("--url", help="base URL of the app; flow URLs are resolved against it")
    run.add_argument("--out", default="truly-done-report")
    run.add_argument("--mine", action="store_true", help="the app is not on this machine but it is yours to test")
    run.add_argument("--allow-origin", action="append", default=[], metavar="HOST",
                     help="let the app send writes to this other host (repeatable)")
    run.add_argument("--offline", action="store_true", help="block every request to another origin")
    run.set_defaults(fn=cmd_run)
    gate = sub.add_parser("gate", help="read reports and exit 0 only when every flow passed")
    gate.add_argument("paths", nargs="+")
    gate.add_argument("--allow-unknown", action="store_true", help="count cannot tell as pass")
    gate.set_defaults(fn=cmd_gate)
    chk = sub.add_parser("check", help="validate flow files without opening a browser")
    chk.add_argument("flows", nargs="+")
    chk.set_defaults(fn=cmd_check)
    snap = sub.add_parser("snapshot", help="list the page's headings and controls, to write a flow from")
    snap.add_argument("url")
    snap.add_argument("--mine", action="store_true")
    snap.set_defaults(fn=cmd_snapshot)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (Refused, FlowError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
