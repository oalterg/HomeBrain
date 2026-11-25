#!/usr/bin/env python3
"""Nighttime software E2E: real HTTP, browser, MCP stdio and controller processes.

Run with the web requirements, requests and playwright installed:
  python scripts/e2e/nighttime.py
Host power/RTC/health boundaries are simulated in the child processes. This
never proves physical wake; see docs/TESTING.md for the hardware release gate.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
PASSWORD = "nighttime-e2e-password"


def write(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(path)


def fixture(root):
    """Replace only host boundaries; schedule, persistence and locks stay real."""
    sys.path.insert(0, str(REPO / "src"))
    import nighttime as nt
    host_file = root / "host.json"
    host = lambda: json.loads(host_file.read_text())
    nt.RTC = root / "wakealarm"
    nt.PACKAGE_LOCKS = (str(root / "dpkg.lock"),)
    nt.time = SimpleNamespace(time=lambda: host()["now"], monotonic=time.monotonic,
                              sleep=lambda _: sys.exit(0))
    nt.boot_id = lambda: host()["boot_id"]
    nt.fingerprint = lambda: host().get("fingerprint", "e2e-hardware")
    nt.capability = lambda: {"available": True, "mode": "poweroff", "board": "E2E host", "hint": "Run a wake test."}
    real_read = nt.read
    nt.read = lambda p, default="": (str(host()["now"] - host()["boot_at"]) + " 0") if str(p) == "/proc/uptime" else real_read(p, default)

    def command(*args, timeout=15):
        current = host()
        if args[:3] == ("systemctl", "enable", "--now") and current.get("timer_fail"):
            raise nt.NighttimeError("Simulated timer installation failure")
        if args[0] == "rtcwake":
            if current.get("alarm_fail"):
                raise nt.NighttimeError("Simulated RTC programming failure")
            nt.RTC.write_text(args[-1])
        if "poweroff" in args:
            with (root / "power_requests.jsonl").open("a") as f:
                f.write(json.dumps({"at": current["now"], "alarm": nt.RTC.read_text()}) + "\n")
        if args[0] == "timedatectl":
            return "no"
        if args[:2] == ("systemctl", "show"):
            return "active" if current.get("busy") == args[2] else "inactive"
        return "active"

    def healthy(expected=None):
        if host().get("unhealthy"):
            raise nt.NighttimeError("Simulated unavailable application")
        return ["db", "redis", "caddy", "nextcloud", "homeassistant", "vaultwarden"]

    nt.command = command
    nt.healthy = healthy
    return nt


def child(mode):
    root = Path(os.environ["INSTALL_DIR"])
    nt = fixture(root)
    if mode == "tick":
        nt.tick()
        return
    import app
    app.app._vault_signups_checked = True
    app.limiter.enabled = False
    app._PLATFORM_RECORD = {"arch": "x86_64", "has_gpu": False, "gpu_driver": "none"}
    app.app.run(host="127.0.0.1", port=int(os.environ["HB_E2E_PORT"]), threaded=True)


def main():
    import requests
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="hb-nighttime-e2e-") as directory:
        root = Path(directory)
        (root / ".setup_complete").touch()
        (root / ".env").write_text(f"MASTER_PASSWORD={PASSWORD}\nMANAGER_PASSWORD={PASSWORD}\nHOMEBRAIN_SELF_NONCE=e2e-nonce\nDEPLOYMENT_MODE=local\n")
        (root / "config").symlink_to(REPO / "config", target_is_directory=True)
        (root / "wakealarm").write_text("")
        (root / "dpkg.lock").touch()
        (root / "tmp").mkdir()
        host_file = root / "host.json"
        epoch = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
        host = {"now": epoch("2026-11-25T20:00:00"), "boot_at": epoch("2026-11-25T18:00:00"), "boot_id": "boot-one"}
        write(host_file, host)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        env = os.environ | {"INSTALL_DIR": str(root), "HOMEBRAIN_STATE_DIR": str(root / "state"),
                            "HOMEBRAIN_HOME": str(root / "home"), "HB_MEDIA_STATE_DIR": str(root / "media"),
                            "TMPDIR": str(root / "tmp"), "HB_E2E_PORT": str(port),
                            "HOMEBRAIN_PENDING_ACTIONS": str(root / "pending.json"),
                            "HOMEBRAIN_AUDIT_DIR": str(root / "audit"), "HOMEBRAIN_BASE_URL": url,
                            "HOMEBRAIN_MCP_CONSENT": "false", "HOMEBRAIN_SELF_TOKEN_FILE": str(root / "token")}
        token = hmac.new(b"e2e-nonce", PASSWORD.encode(), hashlib.sha256).hexdigest()
        (root / "token").write_text(token)
        log = (root / "server.log").open("w+")
        server = None

        def start():
            nonlocal server
            server = subprocess.Popen([sys.executable, __file__, "serve"], env=env, stdout=log, stderr=log)
            for _ in range(100):
                if server.poll() is not None:
                    log.seek(0)
                    raise AssertionError(log.read())
                try:
                    requests.get(url + "/api/system/nighttime", timeout=0.2)
                    return
                except requests.ConnectionError:
                    time.sleep(0.1)
            raise AssertionError("server did not start")

        def advance(**changes):
            host.update(changes)
            write(host_file, host)

        def tick():
            result = subprocess.run([sys.executable, __file__, "tick"], env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr

        session = requests.Session()

        def api(path="", body=None, code=200):
            endpoint = url + "/api/system/nighttime" + path
            r = session.get(endpoint) if body is None else session.post(endpoint, json=body)
            assert r.status_code == code, (r.status_code, r.text)
            return r.json()

        def mcp(name, args=None):
            req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "homebrain." + name, "arguments": args or {}}}
            proc = subprocess.run([sys.executable, str(REPO / "scripts/mcp-homebrain.py")], env=env,
                                  input=json.dumps(req) + "\n", capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr
            return json.loads(json.loads(proc.stdout)["result"]["content"][0]["text"])

        def qualify():
            pending = api("/test", {})["pending"]
            advance(now=pending["start"] + 1)
            tick()
            advance(now=pending["wake"] + 90, boot_at=pending["wake"], boot_id=host["boot_id"] + "-wake")
            tick()
            candidate = api()["test_candidate"]
            api("/confirm-test", {"id": candidate["id"]})

        try:
            start()
            assert requests.get(url + "/api/system/nighttime").status_code == 401
            assert requests.get(url + "/api/integrations/self/nighttime").status_code == 401
            assert session.post(url + "/login", data={"password": PASSWORD}).status_code == 200
            assert not api()["config"]["enabled"]
            assert session.post(url + "/api/system/nighttime/test", data="{}").status_code == 415
            for body in ({"start": "25:00"}, {"timezone": "bad/zone"}, {"enabled": "true"},
                         {"start": "07:00", "end": "07:00"}, {"shell": "poweroff"}):
                api(body=body, code=409)
            api(body={"enabled": True}, code=409)
            api(body={"start": "23:00", "end": "07:00", "timezone": "Europe/Berlin"})
            assert api()["next"] is None
            assert mcp("nighttime_get")["config"]["start"] == "23:00"
            print("PASS: authenticated HTTP/MCP, validation, disabled default and wake-test gate")

            api("/test", {})
            api("/skip", {})
            assert not api()["pending"]
            test = api("/test", {})["pending"]
            advance(now=test["start"] + 1)
            tick()
            assert (root / "wakealarm").read_text() == str(test["wake"])
            advance(now=test["wake"] + 90, boot_at=test["wake"], boot_id="boot-two")
            tick()
            assert not api()["verified"]
            candidate = api()["test_candidate"]
            api("/confirm-test", {"id": "wrong"}, code=409)
            api("/confirm-test", {"id": candidate["id"]})
            assert api()["verified"]
            print("PASS: cancel, RTC arm, simulated cold boot, recovery and explicit test confirmation")

            config = {"enabled": True, "start": "23:00", "end": "07:00", "timezone": "Europe/Berlin"}
            gate = mcp("nighttime_set", config)
            assert gate["requires_confirmation"] and gate["no_auto_confirm"]
            assert not api()["config"]["enabled"]
            assert mcp("nighttime_skip", {"confirmation_token": gate["action_id"]})["ok"] is False
            gate = mcp("nighttime_set", config)
            assert mcp("nighttime_set", config | {"confirmation_token": gate["action_id"]})["ok"]
            assert not mcp("nighttime_set", config | {"confirmation_token": gate["action_id"]})["ok"]
            assert api()["config"]["enabled"]
            gate = mcp("nighttime_skip")
            assert mcp("nighttime_skip", {"confirmation_token": gate["action_id"]})["ok"]
            assert api()["skip_start"]
            advance(now=api()["next"]["start"] + 1)
            tick()
            assert api()["last_result"]["result"] == "skipped"
            assert api()["skip_start"] is None
            print("PASS: MCP explicit consent with auto-consent disabled, action binding, replay rejection, skip")

            old = api()["config"]
            advance(timer_fail=True)
            api(body={"start": "22:00"}, code=409)
            assert api()["config"] == old
            advance(timer_fail=False)
            server.terminate()
            server.wait(5)
            start()
            assert api()["config"] == old

            def blocked():
                # A blocker retries each tick, then skips once the window closes.
                start = api()["next"]["start"]
                advance(now=start + 1)
                tick()
                assert api()["next"]["start"] == start and not api()["pending"]
                advance(now=start + 901)
                tick()
                assert api()["last_result"]["result"] == "skipped" and api()["next"]["start"] > start

            with (root / "state/power.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                blocked()
            advance(busy="apt-daily-upgrade.service")
            blocked()
            advance(busy=None)
            with (root / "dpkg.lock").open("a") as lock:
                fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                blocked()
            advance(busy=None, alarm_fail=True, now=api()["next"]["start"] + 1)
            tick()
            assert not api()["config"]["enabled"]
            assert api()["last_result"]["result"] == "failed"
            assert not api()["verified"]
            print("PASS: timer rollback, manager restart, concurrent-operation barrier, update blocker, RTC failure")

            advance(alarm_fail=False)
            qualify()
            (root / "wakealarm").write_text(str(host["now"] + 86400))
            api("/test", {}, code=409)
            assert (root / "wakealarm").read_text() == str(host["now"] + 86400)
            (root / "wakealarm").write_text("")
            pending = api("/test", {})["pending"]
            advance(now=pending["start"] + 1)
            tick()
            advance(now=pending["wake"] + 601, boot_at=pending["wake"], boot_id="bad-services", unhealthy=True)
            tick()
            assert api()["last_result"]["result"] == "failed"
            assert not api()["verified"]
            advance(unhealthy=False)
            qualify()
            print("PASS: competing RTC alarm preserved and failed recovery revokes verification")

            advance(now=epoch("2027-03-27T21:00:00"))
            spring = api(body=config | {"start": "23:00", "end": "02:30"})["next"]
            assert spring["wake"] == epoch("2027-03-28T01:00:00")
            advance(now=epoch("2027-10-30T20:00:00"))
            autumn = api(body={"end": "02:30"})["next"]
            assert autumn["wake"] == epoch("2027-10-31T00:30:00")
            advance(now=autumn["start"] + 1, boot_id="manual-boot")
            tick()
            assert api()["next"]["start"] > host["now"]
            advance(fingerprint="updated-kernel")
            tick()
            assert not api()["config"]["enabled"]
            print("PASS: DST gap/fold, manual boot stays awake, hardware/kernel change invalidates qualification")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1200, "height": 900})
                assert context.request.post(url + "/login", form={"password": PASSWORD}).ok
                page = context.new_page()
                # Unrelated dashboard services aren't part of the fixture.
                page.route("**/api/**", lambda route: route.continue_() if "/nighttime" in route.request.url else route.fulfill(json={}))
                page.goto(url)
                page.get_by_role("button", name="Settings", exact=True).click()
                page.locator("#nighttime-timezone").fill("Europe/Berlin")
                page.locator("#nighttime-start").fill("22:30")
                page.get_by_role("button", name="Save nighttime", exact=True).click()
                page.wait_for_function("!nighttimeDirty")
                assert api()["config"]["start"] == "22:30"
                assert page.locator("#nighttime-enabled").is_disabled()
                page.get_by_role("button", name="Test automatic wake", exact=True).click()
                page.get_by_role("button", name="Cancel", exact=True).click()
                assert not api()["pending"]
                page.evaluate("document.querySelectorAll('.toast').forEach(el => el.remove())")
                page.screenshot(path="/tmp/homebrain-nighttime-desktop.png", full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path="/tmp/homebrain-nighttime-mobile.png", full_page=True)
                browser.close()
            print("PASS: browser settings save, verification gate, test cancellation, desktop/mobile layout")
            print("Nighttime software E2E passed. Physical RTC wake remains a separate hardware gate.")
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-6000:], file=sys.stderr)
            raise
        finally:
            if server:
                server.terminate()
                server.wait(10)
            log.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1])
    else:
        main()
