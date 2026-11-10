# Flow files

A flow is one JSON file: what a user does, then what must be true. `verify.py check FILE` validates it without
opening a browser.

```json
{
  "name": "add a todo",
  "url": "/",
  "steps": [
    {"do": "type", "target": {"label": "New todo"}, "text": "Buy milk"},
    {"do": "click", "target": {"role": "button", "text": "Add"}},
    {"do": "expect", "expect": "text", "value": "Buy milk", "within": "#list", "claim": "the new todo shows in the list"},
    {"do": "reload"}
  ],
  "claims": [
    {"expect": "text", "value": "Buy milk", "claim": "the todo is still there after a reload"},
    {"expect": "no_console_errors"},
    {"expect": "no_failed_requests"}
  ]
}
```

## Top level

| Key | Meaning |
|---|---|
| `name` | Required. Names the report folder. |
| `url` | Required. A full URL, or a path joined to `--url`. |
| `steps` | What the user does, in order (at most 200). |
| `claims` | What must hold at the end (at most 100). |
| `dialogs` | `accept` or `dismiss` for alert / confirm / prompt. |
| `viewport` | `{"width": 390, "height": 800, "mobile": true}` for a phone-sized run. |
| `screenshot` | `false` to skip `final.png`. |
| `notes` | Free text for people. Ignored. |

## Targets

A target names one element. A plain string is shorthand for `{"text": "..."}`.

| Form | Finds |
|---|---|
| `{"role": "button", "text": "Add"}` | by role and visible name (best for buttons and links) |
| `{"label": "Email"}` | a field by its label, aria-label or placeholder |
| `{"testid": "save"}` | by `data-testid` |
| `{"text": "Sign out"}` | the smallest element showing that text |
| `{"selector": "#list li"}` | by CSS selector (last resort) |

Add `"nth": 0` to pick one when several match. Without it, more than one visible match is **cannot tell**,
never a guess.

## Steps (`do`)

| `do` | Fields | Notes |
|---|---|---|
| `goto` | `url` | A path, or a URL on the app's own origin (or a host named by `--allow-origin`). Any other site is refused. |
| `reload` | | Proves data was really saved. |
| `click` | `target` | A real mouse click at the element's centre. Fails if it is disabled or covered. |
| `type` | `target`, `text` | Clears the field, types the text. `text` may be `{"env": "NAME"}` for a secret. |
| `press` | `key` | Enter, Tab, Escape, Backspace, Delete, Space, arrows, Home, End, PageUp, PageDown. |
| `select` | `target`, `option` | Option by label or value. |
| `check` | `target`, `checked` | Sets a checkbox or radio (default `true`). |
| `hover` | `target` | |
| `wait` | `ms` | At most 10000. Prefer `wait_for`. |
| `wait_for` | `text` or `target` | Waits until it shows. |
| `expect` | any claim | A claim checked at this point of the flow. |

Every step and claim takes `timeout_ms` (default 3000, at most 30000). Claims retry until the timeout, so a slow
app still passes.

## Claims (`expect`)

| `expect` | Fields | Passes when |
|---|---|---|
| `text` | `value`, `within`? | the text is visible (inside the selector, if given) |
| `no_text` | `value`, `within`? | the text is not visible |
| `count` | `selector`, `equals` / `at_least` / `at_most` | that many visible elements match |
| `visible` / `hidden` | `target` | the element is, or is not, shown |
| `value` | `target`, `equals` or `checked` | a field holds that value, or a box is (un)checked |
| `url` / `title` | `matches` (regex) or `equals` | the address or title fits |
| `dialog` | `value` | an alert / confirm showed that text |
| `request` | `url_matches`, `method`?, `status`?, `times`? | the app sent that request (with that status, that many times) |
| `no_request` | `url_matches`, `method`? | no such request was sent |
| `no_console_errors` | `ignore`? | no console error or uncaught exception |
| `no_failed_requests` | `ignore`? | no request failed or answered 4xx / 5xx |

Add `"claim": "plain words"` to any claim; the report shows it.

## Verdicts

- **pass**: every claim held. Exit 0.
- **fail**: a claim was checked and is false, or a step hit a real defect (button disabled or covered). Exit 1.
- **cannot tell**: the check could not decide: app not reachable, target missing or ambiguous, run timed out. Exit 2.
- Refused or a bad flow file: exit 3.

## Safety rules built in

- Loopback targets (`localhost`, `127.0.0.1`, `[::1]`) run freely. Any other host needs `--mine`.
- Writes (POST, PUT, PATCH, DELETE) go only to the app's own origin. Others are blocked and listed in the report.
  `--allow-origin HOST` adds one. `--offline` blocks every request to another origin, reads too.
- Chrome runs headless with a new empty profile that is deleted afterwards. Your own profile is never read.
- Values typed from `{"env": ...}` are replaced by `[redacted]` in every report.
- Nothing is sent anywhere. No usage data, no update check, no account.
