#!/usr/bin/env python3
"""Reproducible benchmark: does the check notice a broken feature?

The fixture todo app runs in 12 variants: 3 healthy (plain, slow network, phone viewport) and 9 with one planted
bug each. Two checks judge every variant:

  http-200   the usual "it works": the page and the API both answer 200
  truly-done  the shipped flow tests/fixtures/flows/add-todo.json, driven in headless Chrome

Usage: python3 bench/run.py [--out bench/results/DATE.json]
Needs Chrome or Chromium. Loopback only; nothing leaves the machine.
"""
import argparse
import datetime
import json
import sys
import tempfile
import http.client
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "truly-done" / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "app"))
import server  # noqa: E402
import verify  # noqa: E402

FLOW = ROOT / "tests" / "fixtures" / "flows" / "add-todo.json"
HEALTHY = [("healthy", "", {}), ("healthy, slow network", "?slow=1", {}),
           ("healthy, phone viewport", "", {"viewport": {"width": 390, "height": 800, "mobile": True}})]
BUGS = [("noop-add", "the Add button does nothing"), ("covered", "an invisible layer covers the button"),
        ("disabled", "the button is disabled"), ("console-error", "a script error on submit"),
        ("api-500", "the API fails but the page shows the item"), ("no-persist", "the item is gone after a reload"),
        ("wrong-count", "the counter is off by one"), ("dup", "the write is sent twice"),
        ("no-clear", "the field keeps the old text")]


def http_200(base: str, query: str) -> str:
    """The baseline: GET the page and the API on the loopback fixture and look only at the status."""
    port = urllib.parse.urlsplit(base).port
    ok = True
    for path in ("/" + query, "/api/todos"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            ok = ok and conn.getresponse().status == 200
        except OSError:
            ok = False
        finally:
            conn.close()
    return verify.PASS if ok else verify.FAIL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows = []
    cases = [(n, q, o, False) for n, q, o in HEALTHY] + [(f"bug: {d}", "?bug=" + b, {}, True) for b, d in BUGS]
    for name, query, over, broken in cases:
        app = server.start()
        try:
            flow = json.loads(FLOW.read_text())
            flow.update(over)
            with tempfile.TemporaryDirectory() as tmp:
                rep = verify.Runner(verify.validate_flow(flow), app.url + "/" + query, out=Path(tmp)).run()
            rows.append({"variant": name, "broken": broken, "http_200": http_200(app.url, query),
                         "truly-done": rep["verdict"], "seconds": rep["seconds"], "why": rep["why"]})
        finally:
            app.shutdown()
            app.server_close()
        print(f"{name:<48} http-200={rows[-1]['http_200']:<5} truly-done={rows[-1]['truly-done']:<11} {rows[-1]['why'][:70]}")
    bugs = [r for r in rows if r["broken"]]
    good = [r for r in rows if not r["broken"]]
    summary = {
        "date": datetime.date.today().isoformat(), "version": verify.VERSION,
        "bugs": len(bugs), "healthy": len(good),
        "truly_done_bugs_caught": sum(r["truly-done"] == verify.FAIL for r in bugs),
        "truly_done_false_alarms": sum(r["truly-done"] != verify.PASS for r in good),
        "http_200_bugs_caught": sum(r["http_200"] == verify.FAIL for r in bugs),
        "http_200_false_alarms": sum(r["http_200"] != verify.PASS for r in good),
        "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2],
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    ok = summary["truly_done_bugs_caught"] == len(bugs) and summary["truly_done_false_alarms"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
