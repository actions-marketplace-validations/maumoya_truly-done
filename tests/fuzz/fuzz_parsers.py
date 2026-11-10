"""Fuzz the code that reads untrusted input: flow files, target URLs, the request policy, WebSocket frames
from the browser, and report redaction.

Run with Atheris:   python3 tests/fuzz/fuzz_parsers.py -max_total_time=60
Without Atheris it replays the seed corpus once, so it never needs a network install.
A crash is any exception other than the documented ones (FlowError, Refused)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "truly-done" / "scripts"))
import cdp  # noqa: E402
import verify  # noqa: E402

SEEDS = [
    (Path(__file__).resolve().parents[1] / "fixtures" / "flows" / "add-todo.json").read_bytes(),
    b'{"name": "x", "url": "/", "steps": [{"do": "click", "target": "Save"}], "claims": [{"expect": "url", "matches": "("}]}',
    b"\x81\x05hello",
    b"\x81\x7e\x00\x03abc",
    b"http://127.0.0.1:8000/a/../b?q=1#frag",
    b"https://user:pw@[::1]:99999/",
    b" ",
    b"k",
    b"//]/",
    b"http://[::1",
    b"k\": [\"x",
]


def one_input(data: bytes) -> None:
    text = data.decode("utf-8", "replace")
    frames, rest = cdp.decode_frames(data)
    assert len(rest) <= len(data)
    for _fin, _op, payload in frames:
        assert len(payload) <= len(data)
    for mine in (False, True):
        try:
            url = verify.check_target(text, mine)
            assert url.startswith(("http://", "https://"))
            assert mine or verify.is_loopback(verify.urllib.parse.urlsplit(url).hostname or "")
        except verify.Refused:
            pass
    origin = "http://127.0.0.1:3000"
    reason = verify.decide(text, "POST", origin)
    assert isinstance(reason, str)
    if not reason and text.lower().startswith(("http:", "https:")):
        assert verify.origin_of(text) == origin, "a write to another origin got through"
    try:
        assert verify.join_url(origin + "/?a=1", text)
    except verify.Refused:
        pass
    assert verify.slug(text) and "/" not in verify.slug(text) and ".." not in verify.slug(text)
    secret = text[:8]
    cleaned = verify.redact([text, {text: 1}], [secret])
    json.dumps(cleaned)  # still a JSON-able object, whatever the secret is
    if secret and not set(secret) & set("[redacted]"):
        assert all(secret not in part for part in (cleaned[0], *cleaned[1])), "a secret survived redaction"
    try:
        verify.validate_flow(json.loads(text))
    except verify.FlowError:
        pass
    except ValueError:  # bad JSON
        pass


def main() -> int:
    try:
        import atheris
    except ImportError:
        for seed in SEEDS:
            one_input(seed)
        print(f"replayed {len(SEEDS)} seeds (install atheris to fuzz)")
        return 0
    atheris.instrument_all()
    atheris.Setup(sys.argv, one_input)
    atheris.Fuzz()
    return 0


if __name__ == "__main__":
    sys.exit(main())
