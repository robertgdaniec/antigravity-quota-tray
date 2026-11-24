"""
tools/tray_engine.py
Praktyczna Trójca (Silnik): Logika biznesowa, agregacja danych i pętla w tle.
Odpytuje Language Server Antigravity oraz Windows Credential Manager (gemini:antigravity).
Zapewnia generowanie ikony pystray, zarządzanie powiadomieniami zasobnika,
obsługę przełączania kont (hot-switch), wyliczanie czasów odnowienia (5h / 168h)
oraz generowanie raportu markdown ACCOUNT_QUOTAS.md.
Czysta architektura SRP bez zewnętrznych modułów komunikacyjnych.
Zgodność ze SPEC-007, ADR-048 oraz ADR-051.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import quota_tracker
from tray_model import ensure_default_desktop

try:
    import pystray._win32 as pystray_util_win32
except Exception:
    pystray_util_win32 = None


def generate_icon_image() -> Image.Image:
    """Generuje wyrazistą, kontrastową ikonę zasobnika z błękitno-cyanowym akcentem Antigravity."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Kontrastowa ciemna baza z wyraźnym obrysem
    draw.ellipse([2, 2, size - 3, size - 3], fill=(20, 20, 24, 255), outline=(0, 150, 255, 255), width=3)
    # Nasycony błękitno-cyanowy pierścień (Antigravity Cyan)
    draw.ellipse([12, 12, size - 13, size - 13], fill=(0, 210, 255, 255))
    # Wyrazisty biały rdzeń dający maksymalną widoczność na ciemnym i jasnym pasku zadań
    draw.ellipse([22, 22, size - 23, size - 23], fill=(255, 255, 255, 255))
    return img


def update_data() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Odświeża dane z Language Servera i Windows Credential Managera w bezpieczny sposób."""
    try:
        registry = quota_tracker.load_registry()
        if "accounts" in registry:
            registry["accounts"] = {
                k: v
                for k, v in registry["accounts"].items()
                if "@" in k and "unknown" not in k.lower()
            }

        # 1. Tożsamość u źródła z Windows Credential Managera
        cred_data = quota_tracker.read_windows_credential()
        if cred_data:
            cred_email = quota_tracker.extract_email_from_blob(cred_data[1])
            if cred_email and "@" in cred_email and "unknown" not in cred_email.lower():
                if registry.get("active_account") != cred_email:
                    registry["active_account"] = cred_email
                    if cred_email not in registry.get("accounts", {}):
                        registry.setdefault("accounts", {})[cred_email] = {
                            "email": cred_email,
                            "name": cred_data[0] or cred_email.split("@")[0],
                            "tier": "Google AI Pro",
                            "gemini_remaining_pct": None,
                            "gemini_reset_time": None,
                            "claude_remaining_pct": 100.0,
                            "claude_reset_time": None,
                            "last_updated": quota_tracker.get_current_utc().isoformat(),
                        }
                    registry["last_check"] = quota_tracker.get_current_utc().isoformat()
                    quota_tracker.save_registry(quota_tracker.STORAGE_PATH, registry)

        # 2. Odpytanie działającego Language Servera o najświeższe limity
        server_info = quota_tracker.detect_language_server()
        if server_info:
            live_status = quota_tracker.fetch_live_quota(server_info)
            if live_status:
                parsed = quota_tracker.parse_quota_data(live_status)
                if parsed:
                    email = parsed.get("email")
                    if email and "@" in email and "unknown" not in email.lower():
                        registry.setdefault("accounts", {})[email] = parsed
                        registry["active_account"] = email
                        registry["last_check"] = quota_tracker.get_current_utc().isoformat()
                        quota_tracker.save_registry(quota_tracker.STORAGE_PATH, registry)

        evaluated = quota_tracker.evaluate_accounts(registry)
        return registry, evaluated
    except Exception:
        return {}, []


def export_markdown_report(target_file: Optional[Path | str] = None) -> Path:
    """Generuje raport markdown stanu limitów i zapisuje go na dysku."""
    reg = quota_tracker.load_registry()
    evaluated = quota_tracker.evaluate_accounts(reg)
    md_content = quota_tracker.render_markdown(evaluated, reg.get("active_account"))
    dest = Path(target_file) if target_file else WORKSPACE_DIR / "ACCOUNT_QUOTAS.md"
    dest.write_text(md_content, encoding="utf-8")
    return dest


def ensure_tray_icon(icon: Any) -> None:
    """Weryfikuje i przywraca ikonę w powłoce Windows Explorer po awarii lub wybudzeniu."""
    if not icon or not getattr(icon, "_hwnd", None) or sys.platform != "win32" or not pystray_util_win32:
        return
    try:
        nid = pystray_util_win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(pystray_util_win32.NOTIFYICONDATAW),
            hWnd=icon._hwnd,
            hID=id(icon),
            uFlags=pystray_util_win32.NIF_TIP,
            szTip=getattr(icon, "title", "")[:127],
        )
        res = pystray_util_win32.Shell_NotifyIcon(pystray_util_win32.NIM_MODIFY, ctypes.byref(nid))
        if not res and getattr(icon, "visible", False):
            icon._show()
    except Exception:
        pass


def refresh_tray(icon: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Aktualizuje etykietę podpowiedzi tooltipu w zasobniku systemowym."""
    reg, evaluated = update_data()
    active_acc = next((a for a in evaluated if a.get("is_active")), None)
    if active_acc:
        short_email = active_acc["email"].split("@")[0]
        g_pct = active_acc["effective_gemini_pct"]
        c_pct = active_acc["effective_claude_pct"]
        title_text = (
            f"⚡ Antigravity [{short_email}]\n"
            f"Gemini: {g_pct:.0f}% | Claude: {c_pct:.0f}%\n"
            "👉 Kliknij LPM, aby otworzyć panel"
        )
    else:
        title_text = "⚡ Antigravity Quota\nKliknij LPM, aby otworzyć panel"

    if icon:
        try:
            icon.title = title_text
        except Exception:
            pass
        ensure_tray_icon(icon)

    return reg, evaluated


def staggered_resume_sync(
    icon: Any,
    is_running_fn: Callable[[], bool],
    on_refresh_callback: Optional[Callable[[], None]] = None,
) -> None:
    """Adaptacyjna sekwencja odświeżania po wybudzeniu ze stanu uśpienia."""
    for delay in (1, 3, 7, 15):
        time.sleep(delay)
        if not is_running_fn():
            break
        try:
            ensure_tray_icon(icon)
            refresh_tray(icon)
            if on_refresh_callback:
                on_refresh_callback()
        except Exception:
            pass


class TrayDataEngine:
    """
    Hermetyczny silnik operacji domenowych, integracji Language Servera oraz zarządzania sesjami.
    """

    @classmethod
    def update_data(cls) -> Dict[str, Any]:
        """Pobiera i scala dane z monitora limitów i Windows Credential Managera."""
        reg, evaluated = update_data()
        return {
            "accounts": reg.get("accounts", {}),
            "raw_registry": reg,
            "evaluated_accounts": evaluated,
        }

    @staticmethod
    def hot_switch_account(target_email: str) -> Tuple[bool, str]:
        """Przełącza aktywne konto w locie (< 500ms) i synchronizuje stan."""
        try:
            ok = quota_tracker.switch_to_account(target_email)
            if ok:
                update_data()
                return True, f"Pomyślnie przełączono na {target_email}"
            return False, f"Nie udało się przełączyć na konto {target_email}"
        except Exception as ex:
            return False, f"Błąd przełączania konta: {str(ex)}"

    @staticmethod
    def capture_current_session() -> Tuple[bool, str]:
        """Zapisuje bieżącą sesję z Windows Credential Managera do bezpiecznego skarbca."""
        try:
            ok = quota_tracker.save_current_session()
            if ok:
                return True, "Pomyślnie zapisano sesję do skarbca"
            return False, "Nie udało się zapisać sesji"
        except Exception as ex:
            return False, f"Błąd zapisu sesji: {str(ex)}"


def format_relative_time(iso_ts: Optional[str]) -> str:
    """Formatowanie względnego czasu dla telemetrii."""
    if not iso_ts:
        return "nigdy"
    try:
        from datetime import datetime, timezone
        clean = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        diff = (datetime.now(timezone.utc) - dt).total_seconds()
        if diff < 60:
            return f"{int(diff)}s temu"
        if diff < 3600:
            return f"{int(diff // 60)}m temu"
        return f"{int(diff // 3600)}h temu"
    except Exception:
        return "nieznany"
