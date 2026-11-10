# Changelog

## 0.1.0

First version.

- `verify.py run`: drives a running web app in headless Chrome from a JSON flow (goto, reload, click, type, press,
  select, check, hover, wait, wait_for, expect) and prints `VERDICT PASS`, `FAIL` or `CANNOT TELL`.
- 13 claim kinds: text, no_text, count, visible, hidden, value, url, title, dialog, request, no_request,
  no_console_errors, no_failed_requests.
- A loopback base URL may be written without a scheme: `--url localhost:3000` means `http://localhost:3000`.
- `verify.py snapshot` (controls on a page), `check` (validate a flow), `gate` (exit 0 only when every report passed).
- Report per flow: `report.json`, `report.md`, `final.png`. Secrets typed from `{"env": "NAME"}` are redacted.
- Guard rails in code: loopback by default (`--mine` for any other host), writes to other origins blocked,
  `--offline`, a fresh browser profile per run, no telemetry, no update check, no account.
- GitHub Action (`uses: maumoya/truly-done@v0.1.0`) with a self-test: one passing run, one failing run.
- Benchmark `bench/run.py`: 9 planted bugs and 3 healthy variants of a fixture app.
