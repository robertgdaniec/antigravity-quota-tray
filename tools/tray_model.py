"""
tools/tray_model.py
Praktyczna Trójca (Model): Struktury Win32, motyw FluentTheme oraz modele stanu UI.
Obsługuje integrację z powłoką Windows 11 (DWM drop shadow, zaokrąglone narożniki, High-DPI),
inteligentne pozycjonowanie flyoutu na konfiguracjach multi-monitor pod kursorem myszy,
zarządzanie pojedynczą instancją z automatycznym odzyskiwaniem (Auto-Recovery)
oraz ścisłe kontrakty danych Pydantic v2.
Zgodność ze SPEC-007, ADR-048 oraz ADR-051.
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

try:
    import psutil
except ImportError:
    psutil = None

# ==============================================================================
# Windows High-DPI Awareness (Per-Monitor DPI Aware v2)
# ==============================================================================
if sys.platform == "win32":
    for mode in [2, 1]:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(mode)
            break
        except Exception:
            pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_dpi_scale() -> float:
    """Zwraca mnożnik skali DPI ekranu głównego (domyślnie 1.0 dla 96 DPI / 100%)."""
    if sys.platform == "win32":
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            return max(1.0, dpi / 96.0)
        except Exception:
            pass
    return 1.0


apply_high_dpi = get_dpi_scale

# ==============================================================================
# DWM API & Win32 Helpers (Windows 11 Fluent Design)
# ==============================================================================
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUNDED = 2

HWND_BROADCAST = 0xFFFF
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007

WM_TOGGLE_FLYOUT = (
    ctypes.windll.user32.RegisterWindowMessageW("AntigravityQuota_ToggleFlyout")
    if sys.platform == "win32"
    else 0
)


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
    ]


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def ensure_default_desktop() -> None:
    """Zapewnia, że proces i okna są podpięte pod aktywny pulpit użytkownika (WinSta0\\Default)."""
    if sys.platform == "win32":
        try:
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            hcurrent = u32.GetThreadDesktop(k32.GetCurrentThreadId())
            buf = ctypes.create_unicode_buffer(256)
            if hcurrent and u32.GetUserObjectInformationW(hcurrent, 2, buf, 512, None):
                if buf.value.lower() != "default":
                    hdef = u32.OpenDesktopW("Default", 0, False, 0x01FF)
                    if hdef:
                        u32.SetThreadDesktop(hdef)
        except Exception:
            pass


def get_toplevel_hwnd(window: Any) -> int:
    """Zwraca właściwy uchwyt HWND okna najwyższego poziomu."""
    try:
        window.update_idletasks()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            wm_f = window.wm_frame()
            if wm_f:
                frame_hwnd = int(wm_f, 16) if str(wm_f).startswith("0x") else int(wm_f)
                if frame_hwnd and ctypes.windll.user32.IsWindow(frame_hwnd):
                    return frame_hwnd
        except Exception:
            pass
        try:
            hwnd = window.winfo_id()
            ancestor = ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOT
            if ancestor and ctypes.windll.user32.IsWindow(ancestor):
                return ancestor
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent and ctypes.windll.user32.IsWindow(parent):
                return parent
            return hwnd
        except Exception:
            pass
    return getattr(window, "winfo_id", lambda: 0)()


def apply_dwm_effects(window: Any) -> None:
    """Aplikuje zaokrąglone rogi Windows 11, Dark Mode i cień DWM."""
    if sys.platform != "win32":
        return
    try:
        hwnd = get_toplevel_hwnd(window)
        if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
            return
        dwm = ctypes.windll.dwmapi
        dark = wintypes.BOOL(True)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), ctypes.sizeof(dark))
        corner = wintypes.DWORD(DWMWCP_ROUNDED)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), ctypes.sizeof(corner))
        margins = MARGINS(1, 1, 1, 1)
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    except Exception:
        pass


def calculate_flyout_position(width: int, height: int) -> Tuple[int, int]:
    """
    Oblicza optymalne współrzędne flyoutu w prawym dolnym rogu aktywnego monitora pod kursorem myszy (SPEC-007).
    Obsługuje konfiguracje multi-monitor z ujemnym lub przesuniętym obszarem roboczym.
    """
    work_r, work_b, work_l, work_t = 1920, 1040, 0, 0
    if sys.platform == "win32":
        try:
            pt = POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                # MONITOR_DEFAULTTONEAREST = 2
                h_monitor = ctypes.windll.user32.MonitorFromPoint(pt, 2)
                if h_monitor:
                    mi = MONITORINFO()
                    mi.cbSize = ctypes.sizeof(MONITORINFO)
                    if ctypes.windll.user32.GetMonitorInfoW(h_monitor, ctypes.byref(mi)):
                        work_r = mi.rcWork.right
                        work_b = mi.rcWork.bottom
                        work_l = mi.rcWork.left
                        work_t = mi.rcWork.top
        except Exception:
            try:
                rect = wintypes.RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
                work_r, work_b, work_l, work_t = rect.right, rect.bottom, rect.left, rect.top
            except Exception:
                pass
    return max(work_l + 10, work_r - width - 16), max(work_t + 10, work_b - height - 16)


# ==============================================================================
# Stałe Stylistyczne FluentTheme (Windows 11 Dark Mode)
# ==============================================================================
class FluentTheme:
    """Spójny system wzornictwa Fluent Design dla widgetu i panelu flyout Antigravity."""

    BG_CANVAS = "#202020"
    BG_WINDOW = "#181818"
    BG_FLYOUT = "#1f1f1f"
    BG_CARD = "#282828"
    BG_CARD_ACTIVE = "#262b27"
    BG_CARD_HOVER = "#2a2a2a"
    BG_SUBTLE = "#2d2d2d"

    BORDER_DEFAULT = "#333333"
    BORDER_ACTIVE = "#107c41"
    BORDER_DIVIDER = "#2d2d2d"

    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "#cccccc"
    TEXT_MUTED = "#888888"
    TEXT_DIMMED = "#555555"

    ACCENT = "#60cdff"
    ACCENT_CYAN = "#00d2ff"
    ACCENT_BLUE = "#0078d4"
    ACCENT_BUTTON = "#005a9e"
    ACCENT_BUTTON_HOVER = "#0067b8"

    STATUS_SUCCESS = "#107c41"
    STATUS_WARNING = "#d83b01"
    STATUS_DANGER = "#e81123"
    STATUS_ALERT = "#f7630c"

    FONT_FAMILY = "Segoe UI"
    FONT_DISPLAY = "Segoe UI Variable Display"
    FONT_TITLE = ("Segoe UI Variable Display", 11, "bold")
    FONT_SUBTITLE = ("Segoe UI", 8)
    FONT_BODY = ("Segoe UI", 9)
    FONT_BODY_BOLD = ("Segoe UI", 9, "bold")
    FONT_MONO = ("Consolas", 9)

    FLYOUT_WIDTH = 430


# ==============================================================================
# Ścieżki i Zarządzanie Pojedynczą Instancją (PID Lock & Auto-Recovery)
# ==============================================================================
CONFIG_DIR = Path.home() / ".gemini" / "antigravity"
PID_FILE = CONFIG_DIR / "tray_widget.pid"
HWND_FILE = CONFIG_DIR / "tray_widget.hwnd"


def release_pid_lock() -> None:
    """Bezpiecznie usuwa pliki blokady instancji procesu."""
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
            if HWND_FILE.exists():
                HWND_FILE.unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(release_pid_lock)


def acquire_pid_lock() -> Tuple[bool, Optional[int]]:
    """
    Zapewnia pojedynczą instancję widżetu z inteligentnym Auto-Recovery (SPEC-007).
    Jeśli proces wskazany w PID_FILE istnieje, ale jego okno HWND nie istnieje (IsWindow == False)
    lub nie odpowiada na ping Win32 w czasie 500 ms (SendMessageTimeoutW), instancja zombie
    jest automatycznie terminowana, blokady usuwane, a nowy proces przejmuje locka.
    """
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if old_pid == os.getpid():
                return True, None

            process_exists = False
            if psutil and psutil.pid_exists(old_pid):
                try:
                    p_name = psutil.Process(old_pid).name().lower()
                    if "python" in p_name:
                        process_exists = True
                except Exception:
                    pass

            if process_exists:
                # Weryfikacja czy HWND istnieje i odpowiada (Auto-Recovery)
                is_zombie = True
                if HWND_FILE.exists():
                    try:
                        raw_hwnd = HWND_FILE.read_text(encoding="utf-8").strip()
                        if raw_hwnd:
                            hwnd = int(raw_hwnd)
                            if sys.platform == "win32":
                                user32 = ctypes.windll.user32
                                if user32.IsWindow(hwnd):
                                    # Ping Win32 SendMessageTimeoutW z timeoutem 500ms
                                    # WM_NULL = 0x0000, SMTO_ABORTIFHUNG = 0x0002
                                    smto_result = wintypes.DWORD()
                                    res = user32.SendMessageTimeoutW(
                                        hwnd, 0x0000, 0, 0, 0x0002, 500, ctypes.byref(smto_result)
                                    )
                                    if res != 0:
                                        is_zombie = False
                            else:
                                is_zombie = False
                    except Exception:
                        is_zombie = True

                if is_zombie:
                    # Automatyczne ubicie procesu zombie i przejęcie locka
                    try:
                        p = psutil.Process(old_pid)
                        p.terminate()
                        try:
                            p.wait(timeout=1)
                        except Exception:
                            p.kill()
                    except Exception:
                        if sys.platform == "win32":
                            try:
                                subprocess.run(
                                    ["taskkill", "/F", "/PID", str(old_pid)],
                                    capture_output=True,
                                    timeout=2,
                                    creationflags=0x08000000,
                                )
                            except Exception:
                                pass
                    time.sleep(0.01)
                    PID_FILE.unlink(missing_ok=True)
                    HWND_FILE.unlink(missing_ok=True)
                else:
                    # Instancja działa sprawnie i odpowiada na komunikaty
                    return False, old_pid
            else:
                # Proces już nie istnieje w systemie
                PID_FILE.unlink(missing_ok=True)
                HWND_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # Przejęcie / zapisanie locka przez bieżący proces
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    return True, None


def handle_existing_instance(old_pid: int) -> bool:
    """Gdy użytkownik uruchamia widżet działający w tle - pyta o restart."""
    try:
        flags = 0x04 | 0x20 | 0x10000 | 0x40000  # MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST
        ret = ctypes.windll.user32.MessageBoxW(
            0,
            f"Widżet Antigravity Quota działa w tle (PID: {old_pid}).\n\nCzy zrestartować i przywrócić panel zasobnika?",
            "⚡ Antigravity Quota Widget",
            flags,
        )
        if ret == 6:  # IDYES
            killed = False
            if psutil and psutil.pid_exists(old_pid):
                try:
                    p = psutil.Process(old_pid)
                    p.kill()
                    p.wait(timeout=2)
                    killed = True
                except Exception:
                    pass
            if not killed and sys.platform == "win32":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(old_pid)],
                        capture_output=True,
                        timeout=3,
                        creationflags=0x08000000,
                    )
                except Exception:
                    pass
            time.sleep(0.3)
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
            return True
    except Exception:
        pass
    return False


# ==============================================================================
# Modele Danych Stanu Flyoutu (Pydantic v2)
# ==============================================================================
class AccountQuotaCard(BaseModel):
    """Model danych pojedynczego konta renderowanego we flyoucie limitów."""

    model_config = ConfigDict(extra="ignore")
    email: str = Field(..., description="Adres e-mail konta.")
    tier: str = Field(default="Google AI Pro", description="Poziom subskrypcji.")
    is_active: bool = Field(default=False, description="Flaga bieżącego konta.")
    has_vault_session: bool = Field(default=False, description="Obecność w sejfie sesji.")
    effective_gemini_pct: float = Field(default=100.0, ge=0.0, le=100.0)
    effective_claude_pct: float = Field(default=100.0, ge=0.0, le=100.0)
    gemini_msg: str = Field(default="Dostępne 100%")
    claude_msg: str = Field(default="Dostępne 100%")
    seconds_to_reset: int = Field(default=0, ge=0)


class FlyoutState(BaseModel):
    """Model stanu wysuwanego menu podręcznego (Flyout)."""

    model_config = ConfigDict(extra="ignore")
    window_open: bool = False


class TaskbarInfo(BaseModel):
    """Informacje o geometrii i krawędzi paska zadań Windows."""

    model_config = ConfigDict(extra="ignore")
    edge: str = "bottom"  # "bottom", "top", "left", "right"
    x: int = 0
    y: int = 0
    rc_work: Tuple[int, int, int, int] = (0, 0, 1920, 1040)


class DeckDepartmentSummary(BaseModel):
    """Podsumowanie stanu pojedynczego działu agentów (Department Manager)."""

    model_config = ConfigDict(extra="ignore")
    department_id: str
    name: str
    active_goals: int = 0
    total_goals: int = 0
    has_alerts: bool = False
    status_label: str = "IDLE"


class DeckGoalItem(BaseModel):
    """Model celu autonomicznego renderowanego w liście operacyjnej Decku."""

    model_config = ConfigDict(extra="ignore")
    goal_id: str
    department_id: str
    title: str
    target_count: int
    current_count: int
    status: str
    deadline: Optional[str] = None
    heartbeat_interval_sec: int = 30
    last_heartbeat: Optional[str] = None
    is_stalled: bool = False
    progress_pct: float = 0.0


class DeckState(BaseModel):
    """Model stanu aktywnego widoku panelu operacyjnego (Deck)."""

    model_config = ConfigDict(extra="ignore")
    current_tab: str = "goals"
    search_query: str = ""
