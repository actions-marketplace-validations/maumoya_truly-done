#!/usr/bin/env python3
"""A tiny todo app for truly-done's own tests. Loopback only, in-memory, standard library.

    server.py [--port 8765]

The page reads ?bug=NAME and misbehaves on purpose (see index.html). `?bug=api-500` and `?bug=no-persist`
are passed to the API as a header so the server can misbehave too.
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


class App(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.todos, self.next_id, self.lock = [], 1, threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def reply(self, status: int, body=b"", kind="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.reply(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/todos":
            with self.server.lock:
                self.reply(200, list(self.server.todos))
        elif path == "/missing":
            self.reply(404, b"not here", "text/plain")
        else:
            self.reply(404, b"", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] == "/api/reset":
            with self.server.lock:
                self.server.todos, self.server.next_id = [], 1
            return self.reply(200, {"ok": True})
        if self.path.split("?")[0] != "/api/todos":
            return self.reply(404, b"", "text/plain")
        bug = self.headers.get("X-Bug", "")
        try:
            size = min(int(self.headers.get("Content-Length", "0")), 10_000)
            title = str(json.loads(self.rfile.read(size) or b"{}").get("title", "")).strip()[:200]
        except (ValueError, AttributeError):
            return self.reply(400, {"error": "bad json"})
        if bug == "api-500":
            return self.reply(500, {"error": "boom"})
        if not title:
            return self.reply(400, {"error": "title is required"})
        with self.server.lock:
            todo = {"id": self.server.next_id, "title": title}
            self.server.next_id += 1
            if bug != "no-persist":
                self.server.todos.append(todo)
        self.reply(201, todo)

    def do_DELETE(self):
        head, _, tail = self.path.split("?")[0].rpartition("/")
        if head != "/api/todos" or not tail.isdigit():
            return self.reply(404, b"", "text/plain")
        with self.server.lock:
            self.server.todos = [t for t in self.server.todos if t["id"] != int(tail)]
        self.reply(204)


def start(port: int = 0) -> App:
    """Start the app on a background thread and return it (tests use this)."""
    app = App(port)
    threading.Thread(target=app.serve_forever, daemon=True).start()
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    app = App(ap.parse_args().port)
    print(f"todo app on {app.url}  (Ctrl-C to stop)")
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
