#!/usr/bin/env python3
"""Dashboard readings for the two discrete cards this box ships with.

An RX 9060 XT and an Arc Pro B60 must publish the same GPU object. The tunnel
badge on a LAN-only box is Deactivated, not a red Stopped: there is no tunnel
process because remote access was not turned on.

    python3 scripts/tests/test_dashboard_hardware.py
    pytest scripts/tests/test_dashboard_hardware.py
"""
import ctypes
import glob
import io
import os
import struct
import sys
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import app as hb            # noqa: E402


def _region(mem_class, total, used):
    # drm_xe_mem_region: class, instance, min_page, total, used,
    # cpu_visible_size, cpu_visible_used, reserved[6]
    return struct.pack("=HHIQQQQ6Q", mem_class, 1, 4096, total, used, total, used,
                       0, 0, 0, 0, 0, 0)


def _blob(regions):
    return struct.pack("=II", len(regions), 0) + b"".join(regions)


def test_local_tunnel_is_deactivated_not_stopped():
    assert hb.present_tunnel_status("stopped", local=True) == "deactivated"
    assert hb.present_tunnel_status("running", local=True) == "running"
    assert hb.present_tunnel_status("unhealthy", local=True) == "unhealthy"


def test_remote_tunnel_stopped_stays_a_fault():
    assert hb.present_tunnel_status("stopped", local=False) == "stopped"


def test_xe_vram_query_ignores_system_memory_and_sums_tiles():
    gib = 1024 ** 3
    blob = _blob([
        _region(0, 32 * gib, 8 * gib),          # system memory
        _region(hb._XE_CLASS_VRAM, 24 * gib, 20 * gib),
        _region(hb._XE_CLASS_VRAM, 1 * gib, int(0.5 * gib)),
    ])
    used, total = hb._xe_vram_totals(blob)
    assert total == 25 * gib
    assert used == 20 * gib + int(0.5 * gib)


def test_xe_vram_query_rejects_a_short_blob():
    assert hb._xe_vram_totals(b"") is None
    assert hb._xe_vram_totals(struct.pack("=II", 1, 0)) is None


# What the dashboard renders for a discrete card. Both readers must fill every key.
_DISCRETE_KEYS = {
    "available", "util_percent", "temp_c", "memory_label",
    "vram_used_gb", "vram_total_gb", "vram_percent",
}


def test_xe_query_struct_matches_the_uapi():
    # The ioctl number embeds this size. A drifted struct is a silent ENOTTY.
    assert ctypes.sizeof(hb._XeDeviceQuery) == 40
    assert hb._XE_REGION.size == 88


def test_intel_stats_publish_discrete_vram():
    gib = 1024 ** 3
    with ExitStack() as stack:
        stack.enter_context(patch.object(hb, "_intel_drm_devices", return_value=[
            ("xe", "/sys/devices/pci0000:00/0000:00:02.0", "/dev/dri/renderD128"),
            ("xe", "/sys/devices/pci0000:00/0000:03:00.0", "/dev/dri/renderD129"),
        ]))
        stack.enter_context(patch.object(
            hb, "_xe_query_vram",
            side_effect=lambda node: (1 * gib, 2 * gib) if node.endswith("128") else (20 * gib, 24 * gib),
        ))
        stack.enter_context(patch.object(
            hb, "_xe_util_percent",
            side_effect=lambda pdev: 7 if pdev.endswith("03:00.0") else 0,
        ))
        stack.enter_context(patch.object(hb, "_intel_pkg_temp", return_value=41.5))
        stack.enter_context(patch.object(hb, "get_platform", return_value={"gpu_memory": "discrete"}))
        stats = hb._gpu_stats_intel()

    assert set(stats) == _DISCRETE_KEYS
    assert stats["vram_used_gb"] == 20.0
    assert stats["vram_total_gb"] == 24.0
    assert stats["vram_percent"] == round(20 / 24 * 100)
    assert stats["util_percent"] == 7
    assert stats["temp_c"] == 41.5
    assert stats["memory_label"] == "VRAM"


def test_intel_stats_settle_when_the_query_has_no_vram():
    with ExitStack() as stack:
        stack.enter_context(patch.object(hb, "_intel_drm_devices", return_value=[
            ("xe", "/sys/devices/pci0000:00/0000:00:02.0", "/dev/dri/renderD128"),
        ]))
        stack.enter_context(patch.object(hb, "_xe_query_vram", return_value=None))
        stack.enter_context(patch.object(hb, "_intel_pkg_temp", return_value=40.0))
        stack.enter_context(patch.object(hb, "get_platform", return_value={"gpu_memory": "unified"}))
        stats = hb._gpu_stats_intel()

    assert stats["available"] is True
    assert "vram_percent" not in stats
    assert stats["temp_c"] == 40.0
    assert stats["memory_label"] == "GPU memory"


def test_amdgpu_stats_skip_the_connector_node_and_fill_the_same_keys():
    gib = 1024 ** 3
    files = {
        "/sys/class/drm/card0-HDMI-A-1/device/mem_info_vram_used": "1\n",
        "/sys/class/drm/card0-HDMI-A-1/device/mem_info_vram_total": "2\n",
        "/sys/class/drm/card1/device/mem_info_vram_used": f"{8 * gib}\n",
        "/sys/class/drm/card1/device/mem_info_vram_total": f"{16 * gib}\n",
        "/sys/class/drm/card1/device/hwmon/hwmon0/temp1_input": "45000\n",
    }

    def fake_glob(pat):
        if pat == "/sys/class/drm/card*/device":
            return [
                "/sys/class/drm/card0-HDMI-A-1/device",
                "/sys/class/drm/card1/device",
            ]
        if pat == "/sys/class/drm/card1/device/hwmon/hwmon*/temp1_input":
            return ["/sys/class/drm/card1/device/hwmon/hwmon0/temp1_input"]
        return []

    def fake_open(path, *args, **kwargs):
        try:
            return io.StringIO(files[path])
        except KeyError:
            raise OSError(path)

    with ExitStack() as stack:
        stack.enter_context(patch.object(glob, "glob", side_effect=fake_glob))
        stack.enter_context(patch.object(os.path, "exists", return_value=True))
        stack.enter_context(patch("builtins.open", fake_open))
        stack.enter_context(patch.object(hb, "_amdgpu_compute_util", return_value=12))
        stats = hb._gpu_stats_amdgpu()

    assert set(stats) == _DISCRETE_KEYS
    assert stats["vram_used_gb"] == 8.0
    assert stats["vram_total_gb"] == 16.0
    assert stats["vram_percent"] == 50
    assert stats["util_percent"] == 12
    assert stats["temp_c"] == 45.0
    assert stats["memory_label"] == "VRAM"


def test_each_card_uses_only_its_own_reader():
    seen = []

    def amd():
        seen.append("amd")
        return {"available": True, "memory_label": "VRAM"}

    def intel():
        seen.append("intel")
        return {"available": True, "memory_label": "VRAM"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(hb, "_gpu_stats_amdgpu", side_effect=amd))
        stack.enter_context(patch.object(hb, "_gpu_stats_intel", side_effect=intel))
        stack.enter_context(patch.object(hb, "get_platform", return_value={"gpu_driver": "amdgpu"}))
        assert hb.get_gpu_stats()["memory_label"] == "VRAM"
        stack.enter_context(patch.object(hb, "get_platform", return_value={"gpu_driver": "xe"}))
        assert hb.get_gpu_stats()["memory_label"] == "VRAM"

    assert seen == ["amd", "intel"]


def test_xe_util_is_the_busiest_engine_over_the_last_interval():
    hb._xe_util_cache.update(ts=0.0, busy={}, total={}, pct=0)
    samples = iter([
        "drm-driver:\txe\n"
        "drm-client-id:\t3\n"
        "drm-pdev:\t0000:03:00.0\n"
        "drm-cycles-rcs:\t0\n"
        "drm-total-cycles-rcs:\t1000\n"
        "drm-cycles-ccs:\t0\n"
        "drm-total-cycles-ccs:\t1000\n"
        "drm-engine-capacity-ccs:\t1\n",
        "drm-driver:\txe\n"
        "drm-client-id:\t3\n"
        "drm-pdev:\t0000:03:00.0\n"
        "drm-cycles-rcs:\t100\n"
        "drm-total-cycles-rcs:\t2000\n"
        "drm-cycles-ccs:\t500\n"
        "drm-total-cycles-ccs:\t2000\n"
        "drm-engine-capacity-ccs:\t1\n",
    ])

    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            return io.StringIO(next(samples))
        return real_open(path, *args, **kwargs)

    with patch.object(glob, "glob", side_effect=lambda pat: ["/proc/9/fdinfo/4"] if "fdinfo" in pat else []), \
            patch("builtins.open", fake_open):
        assert hb._xe_util_percent("0000:03:00.0") == 0
        # ccs moved 500 cycles over a 1000-cycle window; rcs moved 100. Busiest wins.
        assert hb._xe_util_percent("0000:03:00.0") == 50


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except Exception as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    sys.exit(1 if failed else 0)
