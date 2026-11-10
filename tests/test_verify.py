"""Tests for truly-done. Policy and flow validation run anywhere; the end-to-end tests need Chrome."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "truly-done" / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "app"))

import cdp  # noqa: E402
import server  # noqa: E402
import verify as v  # noqa: E402

FLOW = ROOT / "tests" / "fixtures" / "flows" / "add-todo.json"
BUGS = ["noop-add", "covered", "console-error", "api-500", "no-persist", "wrong-count", "disabled", "dup", "no-clear"]
CHROME = cdp.find_chrome()


def flow(**over):
    base = {"name": "t", "url": "/", "steps": [], "claims": [{"expect": "text", "value": "Todos"}]}
    base.update(over)
    return base


class Policy(unittest.TestCase):
    def test_loopback(self):
        for host in ("localhost", "127.0.0.1", "[::1]", "app.localhost", "127.8.9.1"):
            self.assertTrue(v.is_loopback(host), host)
        for host in ("example.com", "192.168.1.5", "localhost.evil.com", "0.0.0.0", ""):
            self.assertFalse(v.is_loopback(host), host)

    def test_target_needs_mine_off_machine(self):
        self.assertEqual(v.check_target("http://localhost:3000", False), "http://localhost:3000/")
        with self.assertRaises(v.Refused):
            v.check_target("https://example.com/", False)
        self.assertEqual(v.check_target("https://example.com/a?b=1#c", True), "https://example.com/a?b=1")
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "localhost:3000", "http://"):
            with self.assertRaises(v.Refused):
                v.check_target(bad, True)

    def test_decide(self):
        o = "http://localhost:3000"
        self.assertEqual(v.decide(o + "/api", "POST", o), "")
        self.assertEqual(v.decide("https://cdn.example/x.js", "GET", o), "")
        self.assertTrue(v.decide("https://api.example/pay", "POST", o))
        self.assertTrue(v.decide("https://api.example/pay", "delete", o))
        self.assertEqual(v.decide("https://api.example/pay", "POST", o, allow=("api.example",)), "")
        self.assertTrue(v.decide("https://cdn.example/x.js", "GET", o, offline=True))
        self.assertEqual(v.decide(o + "/x", "GET", o, offline=True), "")
        self.assertEqual(v.decide("data:text/plain,hi", "GET", o, offline=True), "")
        self.assertTrue(v.decide("file:///etc/passwd", "GET", o))
        self.assertTrue(v.decide("http://localhost:3001/x", "POST", o))  # another port is another origin

    def test_worst(self):
        self.assertEqual(v.worst([]), v.PASS)
        self.assertEqual(v.worst([v.PASS, v.UNKNOWN]), v.UNKNOWN)
        self.assertEqual(v.worst([v.UNKNOWN, v.FAIL, v.PASS]), v.FAIL)

    def test_redact(self):
        out = v.redact({"a": ["typed hunter2 pw", {"u": "http://x/?p=p%40ss"}], "n": 3}, ["hunter2", "p@ss", ""])
        self.assertEqual(out, {"a": ["typed [redacted] pw", {"u": "http://x/?p=[redacted]"}], "n": 3})
        self.assertEqual(v.redact({"a": 1}, []), {"a": 1})
        # a secret that looks like JSON syntax must not break the report (found by the fuzz target)
        self.assertEqual(v.redact({"a": ["x y", True], "n": 3}, [" "]), {"a": ["x[redacted]y", True], "n": 3})
        self.assertEqual(v.redact({"tok-9": 12345, "ok": True}, ["tok-9", "234"]), {"[redacted]": "[redacted]", "ok": True})

    def test_slug(self):
        self.assertEqual(v.slug("Add a Todo!"), "add-a-todo")
        self.assertEqual(v.slug("../../etc"), "etc")
        self.assertEqual(v.slug(""), "flow")


class Flows(unittest.TestCase):
    def test_shipped_flow_is_valid(self):
        f = v.load_flow(str(FLOW))
        self.assertEqual(len(f["steps"]), 8)

    def test_bare_loopback_gets_http_and_nothing_else_does(self):
        self.assertEqual(v.with_scheme("localhost:3000"), "http://localhost:3000")
        self.assertEqual(v.with_scheme("127.0.0.1:8080/app?x=1"), "http://127.0.0.1:8080/app?x=1")
        self.assertEqual(v.with_scheme("[::1]:3000"), "http://[::1]:3000")
        for same in ("http://localhost:3000", "example.com:3000", "localhost.evil.com", "localhost:3000@evil.com", "/path", None):
            self.assertEqual(v.with_scheme(same), same)

    def test_urls_python_cannot_parse_are_refused_not_crashes(self):
        for bad in ("//]/", "http://[::1", "http://127.0.0.1:99999/", "http://127.0.0.1:x/"):
            with self.assertRaises(v.Refused):
                v.check_target(bad, True)
            self.assertTrue(v.decide(bad, "GET", "http://127.0.0.1:3000"))
            self.assertEqual(v.origin_of(bad), "")
        with self.assertRaises(v.Refused):
            v.join_url("http://127.0.0.1:3000/", "//]/")

    def test_goto_never_leaves_the_app(self):
        import types
        me = types.SimpleNamespace(origin="http://127.0.0.1:3000", allow=set(), offline=False)
        for url in ("https://example.com/", "http://127.0.0.1:3001/", "//]/"):
            with self.assertRaises(v.Stop) as stop:
                v.Runner.navigate(me, url, "goto")
            self.assertEqual(stop.exception.verdict, v.UNKNOWN)

    def test_text_shorthand(self):
        f = v.validate_flow(flow(steps=[{"do": "click", "target": "Save"}]))
        self.assertEqual(f["steps"][0]["target"], {"text": "Save"})

    def test_bad_flows(self):
        bad = [
            [], {"name": "x"}, flow(name=""), flow(extra=1), flow(dialogs="maybe"),
            flow(viewport={"width": 10, "height": 10}),
            flow(steps=[{"do": "explode"}]), flow(steps=[{"do": "click"}]),
            flow(steps=[{"do": "click", "target": {}}]),
            flow(steps=[{"do": "click", "target": {"selector": "a", "testid": "b"}}]),
            flow(steps=[{"do": "click", "target": {"selector": "a", "text": "b"}}]),
            flow(steps=[{"do": "click", "target": {"text": "a", "nth": -1}}]),
            flow(steps=[{"do": "click", "target": {"text": "a", "xpath": "//a"}}]),
            flow(steps=[{"do": "type", "target": "a"}]),
            flow(steps=[{"do": "type", "target": "a", "text": {"env": "no good"}}]),
            flow(steps=[{"do": "press", "key": "F13"}]),
            flow(steps=[{"do": "wait", "ms": 999999}]),
            flow(steps=[{"do": "wait_for"}]),
            flow(steps=[{"do": "click", "target": "a", "timeout_ms": 10 ** 9}]),
            flow(steps=[{"do": "x"}] * 201),
            flow(claims=[{"expect": "vibes"}]), flow(claims=[{"expect": "text"}]),
            flow(claims=[{"expect": "count", "selector": "li"}]),
            flow(claims=[{"expect": "count", "selector": "li", "equals": True}]),
            flow(claims=[{"expect": "url", "matches": "("}]),
            flow(claims=[{"expect": "url", "matches": "a", "equals": "b"}]),
            flow(claims=[{"expect": "value", "target": "a"}]),
            flow(claims=[{"expect": "request"}]),
            flow(claims=[{"expect": "no_console_errors", "ignore": ["("]}]),
        ]
        for item in bad:
            with self.assertRaises(v.FlowError, msg=json.dumps(item)[:120]):
                v.validate_flow(item)

    def test_load_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.json"
            p.write_text("{not json")
            with self.assertRaises(v.FlowError):
                v.load_flow(str(p))
            with self.assertRaises(v.FlowError):
                v.load_flow(str(Path(d) / "missing.json"))


class Gate(unittest.TestCase):
    def gate(self, reports, *flags):
        with tempfile.TemporaryDirectory() as d:
            for i, rep in enumerate(reports):
                sub = Path(d) / f"r{i}"
                sub.mkdir()
                (sub / "report.json").write_text(rep if isinstance(rep, str) else json.dumps(rep))
            with contextlib.redirect_stdout(io.StringIO()):
                return v.main(["gate", d, *flags])

    def rep(self, verdict):
        return {"tool": "truly-done", "flow": "f", "verdict": verdict}

    def test_gate(self):
        self.assertEqual(self.gate([self.rep("pass"), self.rep("pass")]), 0)
        self.assertEqual(self.gate([self.rep("pass"), self.rep("fail")]), 1)
        self.assertEqual(self.gate([self.rep("pass"), self.rep("cannot_tell")]), 2)
        self.assertEqual(self.gate([self.rep("pass"), self.rep("cannot_tell")], "--allow-unknown"), 0)
        self.assertEqual(self.gate([self.rep("fail"), self.rep("cannot_tell")], "--allow-unknown"), 1)
        self.assertEqual(self.gate([]), 2)
        self.assertEqual(self.gate(["{broken"]), 2)
        self.assertEqual(self.gate(["[1]"]), 2)
        self.assertEqual(self.gate([{"tool": "other", "verdict": "pass"}]), 2)
        self.assertEqual(self.gate([{"tool": "truly-done", "verdict": "great"}]), 2)

    def test_cli_refusals(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(v.main(["run", str(FLOW), "--url", "https://example.com"]), 3)
            self.assertEqual(v.main(["check", "/nope.json"]), 3)
            self.assertEqual(v.main(["check", str(FLOW)]), 0)
            self.assertEqual(v.main(["snapshot", "https://example.com"]), 3)


@unittest.skipUnless(CHROME, "Chrome is not installed")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.app = server.start()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.tmp.cleanup()

    def run_flow(self, f, query="", **kw):
        f = v.validate_flow(f)
        return v.Runner(f, v.check_target(self.app.url + "/" + query, False), out=Path(self.tmp.name), **kw).run()

    def shipped(self, query="", **over):
        f = json.loads(FLOW.read_text())
        f.update(over)
        return self.run_flow(f, query)

    def test_healthy_app_passes(self):
        rep = self.shipped()
        self.assertEqual(rep["verdict"], v.PASS, rep["why"])
        self.assertEqual(len(rep["claims"]), 8)
        self.assertTrue((Path(self.tmp.name) / "final.png").read_bytes().startswith(b"\x89PNG"))
        self.assertIn("**Verdict: PASS**", v.to_markdown(rep))

    def test_healthy_slow_and_mobile_pass(self):
        self.assertEqual(self.shipped("?slow=1")["verdict"], v.PASS)
        self.app.todos.clear()
        self.assertEqual(self.shipped(viewport={"width": 390, "height": 800, "mobile": True})["verdict"], v.PASS)

    def test_every_seeded_bug_fails(self):
        for bug in BUGS:
            with self.subTest(bug=bug):
                self.app.todos.clear()
                rep = self.shipped("?bug=" + bug)
                self.assertEqual(rep["verdict"], v.FAIL, f"{bug}: {rep['why']}")

    def test_cannot_tell(self):
        rep = self.run_flow(flow(steps=[{"do": "click", "target": "No such button", "timeout_ms": 300}]))
        self.assertEqual(rep["verdict"], v.UNKNOWN)
        self.assertEqual(rep["claims"][0]["verdict"], v.UNKNOWN)
        self.assertEqual(self.run_flow(flow(claims=[]))["verdict"], v.UNKNOWN)          # nothing claimed
        rep = self.run_flow(flow(steps=[{"do": "click", "target": {"selector": "li[", "nth": 0}, "timeout_ms": 200}]))
        self.assertEqual(rep["verdict"], v.UNKNOWN)
        rep = self.run_flow(flow(steps=[{"do": "type", "target": {"label": "New todo"}, "text": "a"},
                                        {"do": "press", "key": "Enter"},
                                        {"do": "type", "target": {"label": "New todo"}, "text": "b"},
                                        {"do": "press", "key": "Enter"},
                                        {"do": "click", "target": {"role": "button", "text": "Delete"}, "timeout_ms": 300}]))
        self.assertEqual(rep["verdict"], v.UNKNOWN)                                      # two Delete buttons
        self.assertIn("2 elements match", rep["why"])

    def test_app_not_running(self):
        f = v.validate_flow(flow())
        rep = v.Runner(f, "http://127.0.0.1:9/").run()
        self.assertEqual(rep["verdict"], v.UNKNOWN)
        self.assertIn("Is it running?", rep["why"])

    def test_error_page_fails(self):
        f = v.validate_flow(flow())
        self.assertEqual(v.Runner(f, self.app.url + "/missing").run()["verdict"], v.FAIL)

    def test_keyboard_select_check_and_secret(self):
        os.environ["TRULY_DONE_TEST_SECRET"] = "s3cret-value"
        rep = self.run_flow(flow(steps=[
            {"do": "type", "target": {"label": "New todo"}, "text": {"env": "TRULY_DONE_TEST_SECRET"}},
            {"do": "press", "key": "Enter"},
            {"do": "type", "target": {"selector": "#title"}, "text": "tiny"},
            {"do": "press", "key": "Enter"},
            {"do": "expect", "expect": "count", "selector": "[data-testid=todo]", "equals": 2},
            {"do": "select", "target": {"label": "Filter"}, "option": "Short only"},
            {"do": "expect", "expect": "count", "selector": "[data-testid=todo]", "equals": 1},
            {"do": "check", "target": {"label": "Hide the list"}},
            {"do": "expect", "expect": "value", "target": {"label": "Hide the list"}, "checked": True},
            {"do": "expect", "expect": "hidden", "target": {"testid": "todo"}},
            {"do": "expect", "expect": "visible", "target": {"role": "heading", "text": "Todos"}},
        ], claims=[{"expect": "title", "equals": "Todos"}, {"expect": "url", "matches": "^http://127"},
                   {"expect": "no_text", "value": "Could not add"},
                   {"expect": "no_request", "url_matches": "example\\.com"}]))
        self.assertEqual(rep["verdict"], v.PASS, json.dumps(rep["claims"], indent=1))
        self.assertNotIn("s3cret-value", json.dumps(rep))

    def test_blocks_writes_to_other_origins(self):
        other = server.start()
        try:
            go = (f"fetch('{other.url}/api/todos',{{method:'POST',body:'{{\"title\":\"leak\"}}'}}).catch(()=>0);"
                  f"fetch('{other.url}/api/todos').catch(()=>0)")
            f = v.validate_flow(flow())
            r = v.Runner(f, self.app.url + "/")
            r.flow["steps"] = [{"do": "wait", "ms": 1}]
            orig = r.do_step

            def step(s):
                r.evaluate(go)
                return orig({"do": "wait", "ms": 600})
            r.do_step = lambda s: step(s) if s["do"] == "wait" else orig(s)
            rep = r.run()
            self.assertEqual(other.todos, [])
            self.assertEqual([b["method"] for b in rep["blocked_requests"]], ["POST"])
        finally:
            other.shutdown()
            other.server_close()

    def test_cli_keeps_the_query_of_the_base_url(self):
        self.assertEqual(v.join_url("http://127.0.0.1:1/?bug=x", "/"), "http://127.0.0.1:1/?bug=x")
        self.assertEqual(v.join_url("http://127.0.0.1:1/?bug=x", "/a?b=1"), "http://127.0.0.1:1/a?b=1")
        self.assertEqual(v.join_url("http://127.0.0.1:1", "/a"), "http://127.0.0.1:1/a")
        out = Path(self.tmp.name) / "q"
        with contextlib.redirect_stdout(io.StringIO()):
            code = v.main(["run", str(FLOW), "--url", self.app.url + "/?bug=noop-add", "--out", str(out)])
        self.assertEqual(code, 1)

    def test_cli_run_and_gate(self):
        out = Path(self.tmp.name) / "out"
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = v.main(["run", str(FLOW), "--url", self.app.url, "--out", str(out), "--offline"])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertEqual(v.main(["gate", str(out)]), 0)
            self.assertEqual(v.main(["snapshot", self.app.url]), 0)
        self.assertIn('"label": "New todo"', buf.getvalue())
        self.assertEqual(json.loads((out / "add-a-todo" / "report.json").read_text())["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
