#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Multi-Account Quota - Windows System Tray Widget & Fluent Flyout
----------------------------------------------------------------------------
Aplet paska zadań Windows z nowoczesnym panelem Windows 11 Fluent Flyout.
Umożliwia podgląd limitów AI (Gemini 5h, Claude, Prompt & Flow Credits),
1-klikowe przełączanie tożsamości w locie (< 500 ms hot-switch)
oraz automatyczne przechwytywanie nowych sesji do bezpiecznego skarbca.

Architektura Praktycznej Trójcy (Widok):
- W 100% bezpieczna wielowątkowość oparta na queue.Queue (eliminacja zakleszczeń po wybudzeniu).
- Multi-Monitor High-DPI positioning pod kursorem myszy.
- Focus-Out Auto-Hide z inteligentnym debounce'em oraz Escape-to-close.
- Czysta separacja SRP: zero zależności od Agent Deck.
Zgodność ze SPEC-007, ADR-048 oraz ADR-051.
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
import pystray
from pystray import Menu, MenuItem as item

# Wymuszenie kodowania UTF-8
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Dołączenie modułów narzędziowych projektu
SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import quota_tracker
import tray_engine
from tray_model import (
    HWND_BROADCAST,
    HWND_FILE,
    PID_FILE,
    WM_POWERBROADCAST,
    WM_TOGGLE_FLYOUT,
    FluentTheme,
    acquire_pid_lock,
    apply_dwm_effects,
    calculate_flyout_position,
    ensure_default_desktop,
    get_dpi_scale,
    get_toplevel_hwnd,
    release_pid_lock,
)

try:
    import psutil
except ImportError:
    psutil = None

# ==============================================================================
# Globalna Bezpieczna Wątkowo Kolejka Dyspozycji GUI (SPEC-007)
# ==============================================================================
GUI_QUEUE: queue.Queue[str] = queue.Queue()
CURRENT_ICON: Optional[pystray.Icon] = None
FLYOUT_WINDOW: Optional[FluentFlyoutWindow] = None
IS_RUNNING = True


# ==============================================================================
# Panel FluentFlyoutWindow (Windows 11 Fluent Design)
# ==============================================================================
class FluentFlyoutWindow:
    """Nowoczesny panel flyout Windows 11 Fluent Design z obsługą in-flight hot-switch."""

    WIDTH = FluentTheme.FLYOUT_WIDTH
    DEFAULT_SUBTITLE = "Multi-Account Quota & Session Switcher"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.is_visible = False
        self._last_hide_time = 0.0
        self.is_busy = False
        self.switch_buttons: Dict[str, tk.Button] = {}
        self.gui_queue: queue.Queue[str] = GUI_QUEUE

        self._setup_window()
        self._build_scaffold()

        # Zapisz HWND do pliku IPC dla natychmiastowego wywołania z zewnątrz
        try:
            target_hwnd = get_toplevel_hwnd(self.root)
            HWND_FILE.parent.mkdir(parents=True, exist_ok=True)
            HWND_FILE.write_text(str(target_hwnd), encoding="utf-8")
        except Exception:
            pass

        # Uruchomienie bezpiecznego procesora kolejki GUI w pętli Tkintera
        self.root.after(40, self._process_gui_queue)

    def _setup_window(self) -> None:
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=FluentTheme.BG_FLYOUT)
        self.root.withdraw()

        # Klawiatura: Escape natychmiast chowa panel
        self.root.bind("<Escape>", lambda e: self.hide())

        # Obsługa zdarzenia FocusOut z debounce'em (auto-hide po kliknięciu poza okno)
        self.root.bind("<FocusOut>", self._on_focus_out)

    def _process_gui_queue(self) -> None:
        """Deterministycznie konsumuje dyspozycje GUI na głównym wątku Tkinter (SPEC-007)."""
        try:
            while not self.gui_queue.empty():
                action = self.gui_queue.get_nowait()
                if action == "toggle_flyout":
                    self.toggle()
                elif action == "show_flyout":
                    self.show()
                elif action == "hide_flyout":
                    self.hide()
                elif action == "refresh_flyout":
                    self.refresh_in_place()
                elif action == "quit":
                    self.root.quit()
        except Exception:
            pass
        finally:
            if IS_RUNNING:
                self.root.after(40, self._process_gui_queue)

    def _on_focus_out(self, event: Optional[Any] = None) -> None:
        """Ukrywa panel z debounce'em, gdy fokus opuszcza okno."""
        def _check_focus():
            if not self.is_visible:
                return
            try:
                focused = self.root.focus_get()
                if focused is None:
                    self.hide()
            except Exception:
                self.hide()

        self.root.after(120, _check_focus)

    def _build_scaffold(self) -> None:
        # 1. Nagłówek okna
        self.header_frame = tk.Frame(self.root, bg=FluentTheme.BG_FLYOUT, padx=16, pady=12)
        self.header_frame.pack(fill="x")

        title_box = tk.Frame(self.header_frame, bg=FluentTheme.BG_FLYOUT)
        title_box.pack(side="left", fill="y")

        self.title_lbl = tk.Label(
            title_box,
            text="⚡ Antigravity Quota",
            font=FluentTheme.FONT_TITLE,
            fg=FluentTheme.TEXT_PRIMARY,
            bg=FluentTheme.BG_FLYOUT,
        )
        self.title_lbl.pack(anchor="w")

        self.subtitle_lbl = tk.Label(
            title_box,
            text=self.DEFAULT_SUBTITLE,
            font=FluentTheme.FONT_SUBTITLE,
            fg=FluentTheme.TEXT_MUTED,
            bg=FluentTheme.BG_FLYOUT,
        )
        self.subtitle_lbl.pack(anchor="w")

        self.close_btn = tk.Label(
            self.header_frame,
            text="✕ Zamknij",
            font=("Segoe UI", 8, "bold"),
            fg=FluentTheme.TEXT_SECONDARY,
            bg="#2a2a2a",
            cursor="hand2",
            padx=8,
            pady=3,
            relief="flat",
        )
        self.close_btn.pack(side="right")
        self.close_btn.bind("<Button-1>", lambda e: self.hide())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(fg="#ffffff", bg="#3a3a3a"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(fg="#999999", bg="#2a2a2a"))

        # Separator nagłówka
        tk.Frame(self.root, height=1, bg=FluentTheme.BORDER_DIVIDER).pack(fill="x")

        # 2. Kontener kart kont (z opcją przewijania)
        self.body_frame = tk.Frame(self.root, bg=FluentTheme.BG_FLYOUT)
        self.body_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(self.body_frame, bg=FluentTheme.BG_FLYOUT, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(self.body_frame, orient="vertical", command=self.canvas.yview, width=6)

        self.cards_container = tk.Frame(self.canvas, bg=FluentTheme.BG_FLYOUT)
        self.cards_window = self.canvas.create_window((0, 0), window=self.cards_container, anchor="nw")

        self.cards_container.bind("<Configure>", self._on_container_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.canvas.pack(side="left", fill="both", expand=True)

        # Obsługa rolki myszy
        self.cards_container.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.cards_container.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # Separator stopki
        tk.Frame(self.root, height=1, bg=FluentTheme.BORDER_DIVIDER).pack(fill="x")

        # 3. Stopka akcji
        self.footer_frame = tk.Frame(self.root, bg=FluentTheme.BG_FLYOUT, padx=14, pady=10)
        self.footer_frame.pack(fill="x")

        actions_row = tk.Frame(self.footer_frame, bg=FluentTheme.BG_FLYOUT)
        actions_row.pack(fill="x")

        self.refresh_btn = tk.Button(
            actions_row,
            text="🔄 Odśwież status teraz",
            font=FluentTheme.FONT_SUBTITLE,
            fg=FluentTheme.TEXT_PRIMARY,
            bg="#2a2a2a",
            activebackground="#383838",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
            command=self.on_refresh_clicked,
        )
        self.refresh_btn.pack(side="left")

        self.minimize_btn = tk.Button(
            actions_row,
            text="📌 Zwiń do paska",
            font=FluentTheme.FONT_SUBTITLE,
            fg=FluentTheme.TEXT_SECONDARY,
            bg="#2a2a2a",
            activebackground="#383838",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
            command=self.hide,
        )
        self.minimize_btn.pack(side="right", padx=(4, 0))

        self.report_btn = tk.Button(
            actions_row,
            text="📊 Raport MD",
            font=FluentTheme.FONT_SUBTITLE,
            fg=FluentTheme.TEXT_SECONDARY,
            bg="#2a2a2a",
            activebackground="#383838",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
            command=self.on_report_clicked,
        )
        self.report_btn.pack(side="right")

        self.vault_btn_frame = tk.Frame(self.footer_frame, bg=FluentTheme.BG_FLYOUT)
        self.vault_btn_frame.pack(fill="x", pady=(6, 0))

        self.save_session_btn = tk.Button(
            self.vault_btn_frame,
            text="💾 Zapisz bieżącą sesję do szybkiego przełącznika",
            font=("Segoe UI", 8, "bold"),
            fg="#ffffff",
            bg=FluentTheme.ACCENT_BUTTON,
            activebackground=FluentTheme.ACCENT_BUTTON_HOVER,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            pady=4,
            cursor="hand2",
            command=self.on_save_session_clicked,
        )

        hint_lbl = tk.Label(
            self.footer_frame,
            text="ESC lub kliknięcie poza oknem ukrywa panel",
            font=("Segoe UI", 7),
            fg=FluentTheme.TEXT_DIMMED,
            bg=FluentTheme.BG_FLYOUT,
        )
        hint_lbl.pack(anchor="w", pady=(6, 0))

    def _on_container_configure(self, event: Any) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: Any) -> None:
        self.canvas.itemconfig(self.cards_window, width=event.width)

    def _on_mousewheel(self, event: Any) -> None:
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def update_geometry(self, account_count: Optional[int] = None) -> None:
        """Dostosowuje wysokość i multi-monitor pozycję flyoutu nad paskiem zadań."""
        self.root.update_idletasks()
        header_h = self.header_frame.winfo_reqheight()
        footer_h = self.footer_frame.winfo_reqheight()
        cards_h = self.cards_container.winfo_reqheight()
        needed_h = header_h + footer_h + cards_h + 16
        clamped_h = min(max(needed_h, 360), 1050)

        if needed_h > 1050:
            self.scrollbar.pack(side="right", fill="y")
        else:
            self.scrollbar.pack_forget()

        x, y = calculate_flyout_position(self.WIDTH, clamped_h)
        self.root.geometry(f"{self.WIDTH}x{clamped_h}+{x}+{y}")

    def show(self) -> None:
        """Otwiera panel flyout i wymusza obecność na pierwszym planie."""
        self.is_visible = True
        self.refresh_in_place()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.focus_force()

        if sys.platform == "win32":
            try:
                target_hwnd = get_toplevel_hwnd(self.root)
                ctypes.windll.user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(target_hwnd)
                apply_dwm_effects(self.root)
                try:
                    HWND_FILE.write_text(str(target_hwnd), encoding="utf-8")
                except Exception:
                    pass
            except Exception:
                pass

    def hide(self) -> None:
        """Ukrywa panel flyout z rejestracją znacznika czasu (debounce)."""
        self._last_hide_time = time.time()
        self.root.withdraw()
        self.is_visible = False

    def toggle(self) -> None:
        """Przełącza widoczność panelu flyout z ochroną przed migotaniem."""
        if self.is_visible:
            self.hide()
        else:
            if time.time() - self._last_hide_time < 0.35:
                return
            self.show()

    def refresh_in_place(self) -> None:
        """Odświeża dane i przerysowuje karty w locie bez zamykania okna."""
        reg, evaluated = tray_engine.update_data()
        self.render_data(reg, evaluated)

    def render_data(self, reg: Dict[str, Any], evaluated: List[Dict[str, Any]]) -> None:
        """Renderuje karty kont oraz metryki limitów wewnątrz otwartego okna."""
        self.switch_buttons = {}
        self.subtitle_lbl.config(text=self.DEFAULT_SUBTITLE, fg=FluentTheme.TEXT_MUTED)

        if evaluated:
            evaluated.sort(
                key=lambda x: (
                    x.get("is_active", False),
                    x.get("effective_gemini_pct", 0.0),
                    -x.get("seconds_to_reset", 0),
                ),
                reverse=True,
            )

        for child in self.cards_container.winfo_children():
            child.destroy()

        if not evaluated:
            empty_box = tk.Frame(self.cards_container, bg=FluentTheme.BG_FLYOUT, pady=30)
            empty_box.pack(fill="x")
            tk.Label(
                empty_box,
                text="Brak zarejestrowanych kont w bazie.\nZaloguj się w Antigravity IDE.",
                font=FluentTheme.FONT_BODY,
                fg=FluentTheme.TEXT_MUTED,
                bg=FluentTheme.BG_FLYOUT,
                justify="center",
            ).pack()
        else:
            for acc in evaluated:
                self._create_account_card(acc)

        self.update_geometry(len(evaluated) if evaluated else 1)

        last_check_str = reg.get("last_check")
        time_suffix = ""
        if last_check_str:
            dt = quota_tracker.parse_iso_time(last_check_str)
            if dt:
                time_suffix = f" ({dt.astimezone().strftime('%H:%M:%S')})"
        self.refresh_btn.config(text=f"🔄 Odśwież status{time_suffix}", state="normal")

        active_acc = next((a for a in evaluated if a.get("is_active")), None)
        if active_acc and not active_acc.get("has_vault_session"):
            self.save_session_btn.pack(fill="x")
        else:
            self.save_session_btn.pack_forget()

        apply_dwm_effects(self.root)

    def _create_account_card(self, acc: Dict[str, Any]) -> None:
        is_active = acc["is_active"]
        card_bg = FluentTheme.BG_CARD_ACTIVE if is_active else FluentTheme.BG_CARD
        border_col = FluentTheme.BORDER_ACTIVE if is_active else FluentTheme.BORDER_DEFAULT

        card_border = tk.Frame(self.cards_container, bg=border_col, padx=1, pady=1)
        card_border.pack(fill="x", padx=12, pady=3)

        card_inner = tk.Frame(card_border, bg=card_bg, padx=10, pady=5)
        card_inner.pack(fill="x")

        top_row = tk.Frame(card_inner, bg=card_bg)
        top_row.pack(fill="x")

        if is_active:
            dot_col = "#4ade80"
        elif acc.get("status") in ("READY", "OK") or acc.get("effective_gemini_pct", 0.0) >= 30.0:
            dot_col = "#22c55e"
        elif acc.get("status") == "WARNING" or acc.get("effective_gemini_pct", 0.0) >= 10.0:
            dot_col = "#f59e0b"
        else:
            dot_col = "#ef4444"
        tk.Label(top_row, text="●", font=("Segoe UI", 10), fg=dot_col, bg=card_bg).pack(side="left", padx=(0, 6))

        email_lbl = tk.Label(
            top_row,
            text=acc["email"],
            font=("Segoe UI", 9, "bold" if is_active else "normal"),
            fg=FluentTheme.TEXT_PRIMARY,
            bg=card_bg,
        )
        email_lbl.pack(side="left")

        if is_active:
            tk.Label(
                top_row,
                text="AKTYWNE",
                font=("Segoe UI", 7, "bold"),
                fg="#4ade80",
                bg="#173e23",
                padx=6,
                pady=1,
            ).pack(side="right")
        elif acc.get("has_vault_session"):
            switch_btn = tk.Button(
                top_row,
                text="⚡ Przełącz",
                font=("Segoe UI", 8, "bold"),
                fg="#ffffff",
                bg=FluentTheme.ACCENT_BLUE,
                activebackground="#106ebe",
                activeforeground="#ffffff",
                relief="flat",
                bd=0,
                padx=8,
                pady=1,
                cursor="hand2",
                command=lambda e=acc["email"]: self.on_switch_clicked(e),
            )
            switch_btn.pack(side="right")
            self.switch_buttons[acc["email"]] = switch_btn
        else:
            tk.Label(
                top_row,
                text="Brak w skarbcu",
                font=("Segoe UI", 7),
                fg="#777777",
                bg=card_bg,
            ).pack(side="right")

        g_row = tk.Frame(card_inner, bg=card_bg)
        g_row.pack(fill="x", pady=(4, 0))

        tk.Label(g_row, text="Gemini 5h:", font=FluentTheme.FONT_SUBTITLE, fg="#999999", bg=card_bg).pack(side="left")

        pct = acc.get("effective_gemini_pct", 0.0)
        bar_col = "#107c41" if pct >= 30.0 else ("#f59e0b" if pct >= 10.0 else "#ef4444")
        bar_canvas = tk.Canvas(g_row, width=80, height=5, bg="#383838", highlightthickness=0, bd=0)
        fill_w = int(80 * (min(max(pct, 0.0), 100.0) / 100.0))
        if fill_w > 0:
            bar_canvas.create_rectangle(0, 0, fill_w, 5, fill=bar_col, width=0)
        bar_canvas.pack(side="left", padx=8)

        tk.Label(g_row, text=acc["gemini_msg"], font=FluentTheme.FONT_SUBTITLE, fg="#dddddd", bg=card_bg).pack(side="left")

        c_row = tk.Frame(card_inner, bg=card_bg)
        c_row.pack(fill="x", pady=(2, 0))

        tk.Label(c_row, text="Claude:", font=FluentTheme.FONT_SUBTITLE, fg="#999999", bg=card_bg).pack(side="left")
        tk.Label(c_row, text=acc["claude_msg"], font=FluentTheme.FONT_SUBTITLE, fg="#cccccc", bg=card_bg).pack(side="left", padx=6)

        p_c = acc.get("prompt_credits")
        f_c = acc.get("flow_credits")
        if (p_c and p_c.get("available") is not None) or (f_c and f_c.get("available") is not None):
            cred_row = tk.Frame(card_inner, bg=card_bg)
            cred_row.pack(fill="x", pady=(2, 0))

            credits_tokens = []
            if p_c and p_c.get("available") is not None:
                avail_k = p_c["available"]
                total_k = p_c.get("monthly", 0)
                credits_tokens.append(f"Prompt: {avail_k}/{total_k}")
            if f_c and f_c.get("available") is not None:
                avail_f = f_c["available"]
                total_f = f_c.get("monthly", 0)
                credits_tokens.append(f"Flow: {avail_f}/{total_f}")

            if credits_tokens:
                tk.Label(
                    cred_row,
                    text=" • ".join(credits_tokens),
                    font=("Segoe UI", 7),
                    fg="#777777",
                    bg=card_bg,
                ).pack(side="left")

    def on_refresh_clicked(self) -> None:
        """Asynchroniczne odświeżenie danych bez zamykania okna."""
        self.refresh_btn.config(text="🔄 Pobieranie...", state="disabled")

        def _worker():
            tray_engine.update_data()
            GUI_QUEUE.put("refresh_flyout")

        threading.Thread(target=_worker, daemon=True).start()

    def on_switch_clicked(self, email: str) -> None:
        """In-Flight Hot-Switch z natychmiastowym restartem Language Servera (< 500 ms)."""
        target_btn = self.switch_buttons.get(email)
        if target_btn:
            try:
                target_btn.config(
                    text="⏳ Przełączam...",
                    bg="#d97706",
                    activebackground="#b45309",
                    state="disabled",
                )
            except Exception:
                pass

        for other_email, btn in self.switch_buttons.items():
            if other_email != email:
                try:
                    btn.config(state="disabled")
                except Exception:
                    pass

        self.subtitle_lbl.config(text=f"⚡ Przełączam tożsamość na: {email}...", fg="#38bdf8")
        self.refresh_btn.config(text="⏳ Przełączanie tożsamości...", state="disabled")

        def _worker():
            quota_tracker.switch_to_account(email)
            time.sleep(0.7)
            tray_engine.update_data()
            GUI_QUEUE.put("refresh_flyout")

        threading.Thread(target=_worker, daemon=True).start()

    def on_save_session_clicked(self) -> None:
        """Zapisuje bieżącą sesję do skarbca i odświeża karty w locie."""
        quota_tracker.save_current_session()
        self.refresh_in_place()
        if CURRENT_ICON:
            tray_engine.refresh_tray(CURRENT_ICON)

    def on_report_clicked(self) -> None:
        on_open_report(None, None)


# ==============================================================================
# Pętla Pystray & Obsługa Zasobnika Systemowego
# ==============================================================================
if sys.platform == "win32":
    try:
        import pystray._win32 as pystray_win32
        import pystray._util.win32 as pystray_util_win32

        _orig_pystray_run = pystray_win32.Icon._run

        def _patched_pystray_run(self):
            ensure_default_desktop()
            return _orig_pystray_run(self)

        pystray_win32.Icon._run = _patched_pystray_run

        _orig_on_notify = pystray_win32.Icon._on_notify

        def _patched_on_notify(self, wparam, lparam):
            # Kliknięcie LPM przekazuje dyspozycję do bezpiecznej kolejki GUI
            if lparam == pystray_util_win32.WM_LBUTTONUP:
                GUI_QUEUE.put("toggle_flyout")
                return 0
            return _orig_on_notify(self, wparam, lparam)

        pystray._win32.Icon._on_notify = _patched_on_notify
    except Exception:
        pass


def on_open_report(icon: Any, item: Any) -> None:
    """Otwiera wygenerowany raport markdown w domyślnej aplikacji."""
    try:
        md_file = tray_engine.export_markdown_report()
        os.startfile(str(md_file))
    except Exception:
        pass


def on_quit(icon: Any, item: Any) -> None:
    """Zamyka aplikację i czyści zasoby."""
    global IS_RUNNING
    IS_RUNNING = False
    if icon:
        icon.stop()
    GUI_QUEUE.put("quit")


def build_menu() -> List[Any]:
    """Podręczne menu pomocnicze pod prawy przycisk myszy (PPM)."""
    return [
        item(
            "⚡ Otwórz panel flyout",
            lambda icon, it: GUI_QUEUE.put("show_flyout"),
            default=True,
        ),
        item(
            "🔄 Odśwież status",
            lambda icon, it: (
                tray_engine.update_data(),
                tray_engine.refresh_tray(icon),
                GUI_QUEUE.put("refresh_flyout"),
            ),
        ),
        item("📊 Otwórz raport (ACCOUNT_QUOTAS.md)", on_open_report),
        Menu.SEPARATOR,
        item("❌ Zamknij widget", on_quit),
    ]


def _on_power_broadcast(wparam: int, lparam: int) -> int:
    """Obsługa zdarzeń zasilania Windows (wybudzenie / uśpienie)."""
    PBT_APMRESUMEAUTOMATIC = 0x0012
    PBT_APMRESUMESUSPEND = 0x0007
    if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
        try:
            if CURRENT_ICON and CURRENT_ICON.visible:
                CURRENT_ICON._show()
                tray_engine.refresh_tray(CURRENT_ICON)
                threading.Thread(
                    target=tray_engine.staggered_resume_sync,
                    args=(CURRENT_ICON, lambda: IS_RUNNING, lambda: GUI_QUEUE.put("refresh_flyout")),
                    daemon=True,
                ).start()
        except Exception:
            pass
    return 1


def background_loop(icon: Any) -> None:
    """Wątek okresowo odświeżający stan w tle i auto-przechwytujący nowe sesje (Zero-Click)."""
    ensure_default_desktop()
    for _ in range(5):
        time.sleep(2)
        if not IS_RUNNING:
            return
        try:
            tray_engine.ensure_tray_icon(icon)
        except Exception:
            pass

    loop_count = 0
    while IS_RUNNING:
        time.sleep(5)
        if not IS_RUNNING:
            break
        loop_count += 1

        try:
            # 1. Zero-Click Session Capture: automatyczny zapis nowego konta do skarbca
            quota_tracker.auto_capture_session_if_new()

            # 2. Szybka detekcja zmiany tożsamości w Windows Credential Managerze
            cred_data = quota_tracker.read_windows_credential()
            if cred_data:
                cred_email = quota_tracker.extract_email_from_blob(cred_data[1])
                reg = quota_tracker.load_registry()
                if cred_email and cred_email != reg.get("active_account"):
                    tray_engine.refresh_tray(icon)
                    GUI_QUEUE.put("refresh_flyout")

            # 3. Pełne odpytanie Language Servera co 60 sekund
            if loop_count % 12 == 0:
                tray_engine.refresh_tray(icon)
                GUI_QUEUE.put("refresh_flyout")

            tray_engine.ensure_tray_icon(icon)
        except Exception:
            pass


def on_ready(icon: Any) -> None:
    """Callback wywoływany po pomyślnej instalacji ikony w zasobniku."""
    ensure_default_desktop()
    icon.visible = True

    if hasattr(icon, "_message_handlers"):
        icon._message_handlers[WM_POWERBROADCAST] = _on_power_broadcast
        if WM_TOGGLE_FLYOUT:
            icon._message_handlers[WM_TOGGLE_FLYOUT] = lambda w, l: (GUI_QUEUE.put("show_flyout"), 1)[1]

    try:
        tray_engine.refresh_tray(icon)
    except Exception:
        pass

    bg_thread = threading.Thread(target=background_loop, args=(icon,), daemon=True)
    bg_thread.start()


def main() -> None:
    """Główny punkt startowy apletu zasobnika."""
    ensure_default_desktop()
    lock_acquired, old_pid = acquire_pid_lock()
    if not lock_acquired and old_pid:
        if "--silent" not in sys.argv:
            # Direct Hard-Launch przez HWND IPC
            if HWND_FILE.exists():
                try:
                    raw_hwnd = HWND_FILE.read_text(encoding="utf-8").strip()
                    if raw_hwnd:
                        target_hwnd = int(raw_hwnd)
                        if sys.platform == "win32" and ctypes.windll.user32.IsWindow(target_hwnd):
                            ctypes.windll.user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE
                            ctypes.windll.user32.SetForegroundWindow(target_hwnd)
                except Exception:
                    pass

            if WM_TOGGLE_FLYOUT and sys.platform == "win32":
                ctypes.windll.user32.PostMessageW(HWND_BROADCAST, WM_TOGGLE_FLYOUT, 0, 0)

        sys.exit(0)

    global CURRENT_ICON, FLYOUT_WINDOW

    root = tk.Tk()
    if sys.platform == "win32":
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    FLYOUT_WINDOW = FluentFlyoutWindow(root)

    icon_img = tray_engine.generate_icon_image()
    CURRENT_ICON = pystray.Icon(
        name="AntigravityQuotaWidget",
        icon=icon_img,
        title="Wczytywanie limitów...",
        menu=Menu(build_menu),
    )

    CURRENT_ICON.run_detached(setup=on_ready)

    if "--silent" not in sys.argv or "--show" in sys.argv:
        root.after(150, FLYOUT_WINDOW.show)

    try:
        root.mainloop()
    finally:
        release_pid_lock()


if __name__ == "__main__":
    main()
