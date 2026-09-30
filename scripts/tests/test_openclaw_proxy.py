"""The /openclaw proxy as OpenClaw 2026.8's trusted proxy.

The gateway runs in gateway.auth.mode "trusted-proxy" and believes whatever
identity and client address the manager forwards. These tests pin what the
proxy sends and what it refuses to pass on.

Runnable two ways (needs Flask — install requirements.txt first):
    python3 scripts/tests/test_openclaw_proxy.py
    pytest scripts/tests/test_openclaw_proxy.py
"""
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402


class _Raw:
    headers = {"Content-Type": "application/javascript"}


class _Upstream:
    status_code = 200
    raw = _Raw()

    def iter_content(self, chunk_size=None):
        yield b"ok"

    def close(self):
        pass


@contextmanager
def _proxy(authenticated=True):
    """A test client whose upstream calls are recorded, not made."""
    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        return _Upstream()

    saved = (hb.requests.request, hb.limiter.enabled)
    hb.requests.request = fake_request
    hb.limiter.enabled = False
    hb.app.config["TESTING"] = True
    client = hb.app.test_client()
    if authenticated:
        with client.session_transaction() as sess:
            sess["authenticated"] = True
    try:
        yield client, calls
    finally:
        hb.requests.request, hb.limiter.enabled = saved


def test_forwards_the_owner_and_the_browser_address():
    with _proxy() as (client, calls):
        r = client.get("/openclaw/assets/index.js",
                       headers={"X-Forwarded-For": "192.168.178.49",
                                "X-Forwarded-Proto": "https"},
                       environ_base={"REMOTE_ADDR": "172.18.0.2"})
        assert r.status_code == 200
        sent = calls[0]["headers"]
        assert sent["x-homebrain-user"] == "owner"
        # Caddy's view of the client, not the Docker bridge it reached us over:
        # the gateway refuses a trusted-proxy request whose client is loopback.
        assert sent["X-Forwarded-For"] == "192.168.178.49"
        assert sent["X-Forwarded-Proto"] == "https"
        assert calls[0]["url"] == "http://127.0.0.1:18789/openclaw/assets/index.js"


def test_the_browser_cannot_name_itself_or_its_address():
    with _proxy() as (client, calls):
        client.get("/openclaw/",
                   headers={"x-homebrain-user": "mallory",
                            "X-Real-IP": "10.9.9.9",
                            "Forwarded": "for=10.9.9.9",
                            "x-openclaw-scopes": "operator.read"},
                   environ_base={"REMOTE_ADDR": "192.168.178.49"})
        sent = {k.lower(): v for k, v in calls[0]["headers"].items()}
        assert sent["x-homebrain-user"] == "owner"
        assert sent["x-forwarded-for"] == "192.168.178.49"
        for dropped in ("x-real-ip", "forwarded", "x-openclaw-scopes"):
            assert dropped not in sent, dropped


def test_only_encodings_the_proxy_can_decode_are_offered():
    # 2026.8 answers `br` with Brotli, which requests cannot decode, and the
    # proxy drops Content-Encoding: the browser got raw bytes as text/html.
    with _proxy() as (client, calls):
        client.get("/openclaw/", headers={"Accept-Encoding": "gzip, deflate, br, zstd"})
        assert calls[0]["headers"]["Accept-Encoding"] == "gzip, deflate"


def test_the_owner_avatar_never_reaches_the_gateway():
    # Each trusted-proxy HTTP request makes 2026.8 announce sessions.changed;
    # the Control UI answers by re-fetching the avatar, ~85 times a second.
    with _proxy() as (client, calls):
        r = client.get("/openclaw/api/users/e69ddf73/avatar?v=1790770847508")
        assert r.status_code == 200
        assert r.mimetype == "image/svg+xml"
        assert b">O</text>" in r.data
        assert calls == []


def test_a_signed_out_browser_is_not_proxied():
    with _proxy(authenticated=False) as (client, calls):
        client.get("/openclaw/")
        assert calls == []


if __name__ == "__main__":
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
