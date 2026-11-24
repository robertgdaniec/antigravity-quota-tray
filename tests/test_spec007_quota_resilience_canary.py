#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_spec007_quota_resilience_canary.py
Złoty Kanarek SPEC-007: Architektura Odporności SRE, Live Quota Engine, Multi-Monitor i Separacja SRP.
Standard Poczwórnej Asercji (Quadruple Assertion) ze ścisłym budżetem telemetrii (< 50ms).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import MagicMock, patch

# Wymuszenie kodowania UTF-8
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = WORKSPACE_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import quota_tracker
import tray_engine
import tray_model


def run_spec007_canary() -> dict:
    start_total_ns = time.perf_counter_ns()
    timings: dict[str, float] = {}
    receipt_path = WORKSPACE_DIR / ".canary_receipt.json"
    spec_receipt_path = WORKSPACE_DIR / ".canary_receipt_spec007.json"

    # ==========================================================================
    # ASERCJA 1: Modern LS Detection (CSRF z mocka environ/TCP) (< 15 ms)
    # ==========================================================================
    t0 = time.perf_counter_ns()

    class MockConn:
        def __init__(self, port: int, status: str):
            self.status = status
            self.laddr = MagicMock(port=port)

    class MockProcLS:
        def __init__(self, pid: int = 4321):
            self.info = {
                "pid": pid,
                "name": "language_server.exe",
                "cmdline": ["language_server.exe", "--port", "45678"],
            }

        def net_connections(self, kind: str = "tcp"):
            return [MockConn(45678, quota_tracker.psutil.CONN_LISTEN if quota_tracker.psutil else "LISTEN")]

        def environ(self):
            return {}

    class MockProcChild:
        def __init__(self, pid: int = 1234):
            self.info = {
                "pid": pid,
                "name": "antigravity.exe",
                "cmdline": ["node.exe", "main.mjs", "--child"],
            }

        def net_connections(self, kind: str = "tcp"):
            return []

        def environ(self):
            return {"ANTIGRAVITY_CSRF_TOKEN": "mock-token-modern-spec007"}

    # 1.1 Wielowektorowa detekcja Antigravity 2.34+ (Wektor 1 environ + TCP LISTEN)
    with patch.object(quota_tracker, "psutil") as mock_psutil:
        mock_psutil.CONN_LISTEN = "LISTEN"
        mock_psutil.process_iter.return_value = [MockProcChild(), MockProcLS()]

        detected = quota_tracker.detect_language_server()
        assert detected is not None, "Oczekiwano pomyślnego wykrycia Language Servera"
        assert detected["token"] == "mock-token-modern-spec007", "Token CSRF powinien pochodzić z environ procesów potomnych"
        assert detected["pid"] == 4321, "PID powinien odpowiadać procesowi language_server"
        assert 45678 in detected["ports"], "Port 45678 powinien znajdować się w wykrytych portach TCP LISTEN"

    # 1.2 Graceful fallback w przypadku offline IDE (zero unhandled exceptions)
    with patch.object(quota_tracker, "psutil") as mock_psutil_empty:
        mock_psutil_empty.process_iter.return_value = []
        with patch.dict(os.environ, {}, clear=True):
            offline_result = quota_tracker.detect_language_server()
            assert offline_result is None, "W przypadku braku procesów funkcja powinna bezpiecznie zwrócić None"

    t1 = time.perf_counter_ns()
    timings["assertion_1_ls_detection_ms"] = round((t1 - t0) / 1_000_000, 3)

    # ==========================================================================
    # ASERCJA 2: Auto-Recovery Lock (Przejęcie po martwym HWND) (< 15 ms)
    # ==========================================================================
    t0 = time.perf_counter_ns()

    mock_pid = 8888
    mock_hwnd = 999999

    mock_pid_file = MagicMock()
    mock_pid_file.exists.return_value = True
    mock_pid_file.read_text.return_value = str(mock_pid)

    mock_hwnd_file = MagicMock()
    mock_hwnd_file.exists.return_value = True
    mock_hwnd_file.read_text.return_value = str(mock_hwnd)

    with patch.object(tray_model, "PID_FILE", mock_pid_file), \
         patch.object(tray_model, "HWND_FILE", mock_hwnd_file), \
         patch.object(tray_model.time, "sleep"), \
         patch.object(tray_model, "psutil") as mock_psutil_lock:

        mock_proc_zombie = MagicMock()
        mock_proc_zombie.name.return_value = "python.exe"
        mock_psutil_lock.pid_exists.return_value = True
        mock_psutil_lock.Process.return_value = mock_proc_zombie

        # Symulacja Win32: IsWindow zwraca 0 (okno nie istnieje - instancja zombie)
        if sys.platform == "win32":
            with patch.object(ctypes.windll.user32, "IsWindow", return_value=0):
                acquired, old_pid = tray_model.acquire_pid_lock()
                assert acquired is True, "Nowy proces powinien przejąć locka po wykryciu martwego HWND"
                assert old_pid is None, "Zwrócony old_pid powinien być None po auto-recovery"
                assert mock_proc_zombie.terminate.called or mock_proc_zombie.kill.called, "Proces zombie powinien zostać ubity"
                assert mock_pid_file.write_text.called, "PID_FILE powinien zostać zaktualizowany o PID bieżącego procesu"
        else:
            acquired, old_pid = tray_model.acquire_pid_lock()
            assert acquired is True

    t1 = time.perf_counter_ns()
    timings["assertion_2_auto_recovery_ms"] = round((t1 - t0) / 1_000_000, 3)

    # ==========================================================================
    # ASERCJA 3: Multi-Monitor Coordinates (Monitor Pomocniczy & Offset) (< 10 ms)
    # ==========================================================================
    t0 = time.perf_counter_ns()

    # Symulacja monitora pomocniczego z ujemnym obszarem roboczym:
    # Monitor 1 (Secondary): [-1920, 0] do [0, 1080]
    # Monitor 2 (Primary):   [0, 0] do [1920, 1040]
    width, height = 430, 600

    if sys.platform == "win32":
        # Mock Win32 GetCursorPos, MonitorFromPoint oraz GetMonitorInfoW
        def mock_get_cursor_pos(pt_ref):
            pt_ref._obj.x = -500
            pt_ref._obj.y = 500
            return 1

        def mock_get_monitor_info(h_mon, mi_ref):
            mi_ref._obj.rcWork.left = -1920
            mi_ref._obj.rcWork.top = 0
            mi_ref._obj.rcWork.right = 0
            mi_ref._obj.rcWork.bottom = 1080
            return 1

        with patch.object(ctypes.windll.user32, "GetCursorPos", side_effect=mock_get_cursor_pos), \
             patch.object(ctypes.windll.user32, "MonitorFromPoint", return_value=12345), \
             patch.object(ctypes.windll.user32, "GetMonitorInfoW", side_effect=mock_get_monitor_info):

            calc_x, calc_y = tray_model.calculate_flyout_position(width, height)
            expected_x = 0 - width - 16  # -446
            expected_y = 1080 - height - 16  # 464
            assert calc_x == expected_x, f"Oczekiwano x={expected_x}, otrzymano {calc_x}"
            assert calc_y == expected_y, f"Oczekiwano y={expected_y}, otrzymano {calc_y}"
    else:
        calc_x, calc_y = tray_model.calculate_flyout_position(width, height)
        assert calc_x > 0 and calc_y > 0

    t1 = time.perf_counter_ns()
    timings["assertion_3_multimonitor_coords_ms"] = round((t1 - t0) / 1_000_000, 3)

    # ==========================================================================
    # ASERCJA 4: Purity & Telemetry Budget (Brak agent_hub, Czas < 50ms) (< 10 ms)
    # ==========================================================================
    t0 = time.perf_counter_ns()

    # 4.1 Sprawdzenie czystości kodu źródłowego tray_engine.py
    engine_source = (TOOLS_DIR / "tray_engine.py").read_text(encoding="utf-8")
    forbidden_modules = ["agent_hub", "agent_bridge", "deck_server", "agent_deck", "agent_hub.db"]
    for mod in forbidden_modules:
        assert mod not in engine_source, f"Naruszenie czystości SRP: znaleziono referencję do '{mod}' w tray_engine.py!"

    # 4.2 Sprawdzenie interfejsu TrayDataEngine
    assert hasattr(tray_engine.TrayDataEngine, "update_data"), "Brak metody update_data w TrayDataEngine"
    assert hasattr(tray_engine.TrayDataEngine, "hot_switch_account"), "Brak metody hot_switch_account w TrayDataEngine"
    assert hasattr(tray_engine.TrayDataEngine, "capture_current_session"), "Brak metody capture_current_session w TrayDataEngine"

    t1 = time.perf_counter_ns()
    timings["assertion_4_purity_check_ms"] = round((t1 - t0) / 1_000_000, 3)

    total_time_ms = round((time.perf_counter_ns() - start_total_ns) / 1_000_000, 3)
    timings["total_execution_ms"] = total_time_ms

    assert total_time_ms < 50.0, f"Przekroczono budżet telemetrii kanarka: {total_time_ms} ms >= 50.0 ms"

    receipt = {
        "spec": "SPEC-007",
        "verdict": "PASS",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "timings": timings,
        "assertions": {
            "assertion_1_ls_detection": "PASS",
            "assertion_2_auto_recovery": "PASS",
            "assertion_3_multimonitor_coords": "PASS",
            "assertion_4_purity_and_budget": "PASS",
        },
    }

    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, ensure_ascii=False)

    with open(spec_receipt_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, ensure_ascii=False)

    return receipt


def test_spec007_canary():
    receipt = run_spec007_canary()
    assert receipt["verdict"] == "PASS"
    assert receipt["timings"]["total_execution_ms"] < 50.0


if __name__ == "__main__":
    res = run_spec007_canary()
    print("=" * 60)
    print(" 🚀 ZŁOTY KANAREK SPEC-007: STATUS PASS")
    print(f" Czas wykonania: {res['timings']['total_execution_ms']} ms (Budżet: < 50.0 ms)")
    print("=" * 60)
