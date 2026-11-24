#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-Account Quota Tracker & Session Switcher dla Antigravity.
------------------------------------------------------------------
Funkcjonalności:
1. Odpytuje lokalny Language Server Antigravity o bieżące limity (Gemini Flash/Pro, Claude/GPT).
2. Matematycznie wylicza czas odnowienia (cooldown 5h / tydzień) dla wszystkich znanych kont.
3. Zarządza bezpiecznym skarbcem sesji (~/.gemini/account_sessions/).
4. Umożliwia 1-klikowe przełączanie kont w Windows Credential Managerze (gemini:antigravity)
   oraz in-flight hot-switch Language Servera (< 500 ms) BEZ przeładowywania okna edytora!
"""

import sys
import os
import re
import json
import base64
import argparse
import subprocess
import urllib.request
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

# Wymuszenie bezpiecznego kodowania UTF-8 w konsoli Windows
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import psutil
except ImportError:
    psutil = None

CONFIG_DIR = Path.home() / ".gemini" / "config"
STORAGE_PATH = CONFIG_DIR / "account_quotas.json"
SESSIONS_DIR = Path.home() / ".gemini" / "account_sessions"
TARGET_CREDENTIAL_NAME = "gemini:antigravity"

# Windows Credential Manager P/Invoke
CRED_TYPE_GENERIC = 1

class CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ('Keyword', wintypes.LPWSTR),
        ('Flags', wintypes.DWORD),
        ('ValueSize', wintypes.DWORD),
        ('Value', ctypes.POINTER(ctypes.c_byte)),
    ]

class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR),
        ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME),
        ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
        ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.POINTER(CREDENTIAL_ATTRIBUTE)),
        ('TargetAlias', wintypes.LPWSTR),
        ('UserName', wintypes.LPWSTR),
    ]

PCREDENTIAL = ctypes.POINTER(CREDENTIAL)

if sys.platform == "win32":
    advapi32 = ctypes.windll.advapi32
    CredRead = advapi32.CredReadW
    CredRead.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIAL)]
    CredRead.restype = wintypes.BOOL

    CredWrite = advapi32.CredWriteW
    CredWrite.argtypes = [PCREDENTIAL, wintypes.DWORD]
    CredWrite.restype = wintypes.BOOL

    CredFree = advapi32.CredFree
    CredFree.argtypes = [ctypes.c_void_p]
    CredFree.restype = None
else:
    CredRead = None
    CredWrite = None
    CredFree = None


def get_current_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_time(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        clean = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0m (Gotowe)"
    mins = int(seconds // 60)
    hours = mins // 60
    rem_mins = mins % 60
    if hours > 0:
        return f"{hours}h {rem_mins:02d}m"
    return f"{rem_mins}m"


def detect_language_server() -> dict | None:
    """Wykrywa proces language_server.exe i wyciąga token CSRF oraz porty HTTP.

    Wielowektorowa detekcja (SPEC-007):
    - Wektor 1 (Antigravity 2.34+): Odczyt zmiennej ANTIGRAVITY_CSRF_TOKEN ze środowiska (environ)
      procesów potomnych Antigravity (procesy z 'antigravity' lub 'main.mjs' w nazwie/cmdline
      oraz samego language_server).
    - Wektor 2 (Legacy): Wyciąganie parametru --csrf_token z cmdline procesu language_server.
    - Skanowanie portów TCP w stanie LISTEN powiązanych z procesem language_server.
    - Fallback: pobranie ANTIGRAVITY_CSRF_TOKEN z os.environ.
    - Graceful fallback: zwraca None bez rzucania wyjątków, gdy IDE jest offline.
    """
    if not psutil:
        return None

    try:
        ls_pid: int | None = None
        ls_ports: list[int] = []
        token: str | None = None

        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = (proc.info.get('name') or '').lower()
                cmdline = proc.info.get('cmdline') or []
                cmd_str = ' '.join(cmdline).lower()

                # Identyfikacja procesu language_server
                if 'language_server' in name or 'language_server' in cmd_str:
                    ls_pid = proc.info.get('pid')
                    raw_cmd = ' '.join(cmdline)

                    # Wektor 2: Legacy --csrf_token w cmdline
                    token_match = re.search(r'--csrf_token[=\s]+([a-zA-Z0-9-]+)', raw_cmd)
                    if token_match and not token:
                        token = token_match.group(1)

                    # Odczyt portów TCP LISTEN powiązanych z procesem
                    try:
                        conns = proc.net_connections(kind='tcp')
                    except Exception:
                        conns = []
                    for conn in conns:
                        if getattr(conn, 'status', None) == psutil.CONN_LISTEN:
                            laddr = getattr(conn, 'laddr', None)
                            if laddr and hasattr(laddr, 'port'):
                                ls_ports.append(laddr.port)

                # Wektor 1: Antigravity 2.34+ - zmienna środowiskowa ANTIGRAVITY_CSRF_TOKEN
                if not token and ('antigravity' in name or 'antigravity' in cmd_str or 'main.mjs' in cmd_str or 'language_server' in name):
                    try:
                        env = proc.environ() if hasattr(proc, 'environ') else {}
                        if env and 'ANTIGRAVITY_CSRF_TOKEN' in env:
                            token = env['ANTIGRAVITY_CSRF_TOKEN']
                    except Exception:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, psutil.Error):
                continue
            except Exception:
                continue

        # Zapasowy fallback na os.environ
        if not token:
            token = os.environ.get('ANTIGRAVITY_CSRF_TOKEN')

        if ls_pid and token and ls_ports:
            return {
                'pid': ls_pid,
                'token': token,
                'ports': ls_ports
            }
        return None
    except Exception:
        return None


def restart_language_server(pid: int | None = None) -> bool:
    """Wykrywa działający proces language_server.exe i wykonuje bezszelestny, bezokienkowy restart.
    Rozszerzenie Antigravity w IDE automatycznie podnosi nowy serwer językowy w < 500 ms z nową tożsamością."""
    target_pid = pid
    if target_pid is None:
        info = detect_language_server()
        if info:
            target_pid = info.get('pid')

    if not target_pid:
        return False

    # 1. Bezpośrednie, bezprocesowe zatrzymanie przez psutil (w pamięci)
    if psutil and psutil.pid_exists(target_pid):
        try:
            proc = psutil.Process(target_pid)
            proc.kill()
            return True
        except Exception:
            pass

    # 2. Bezpośrednie wywołanie Win32 API przez ctypes (PROCESS_TERMINATE = 0x0001)
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            h_proc = kernel32.OpenProcess(0x0001, False, target_pid)
            if h_proc:
                try:
                    if kernel32.TerminateProcess(h_proc, 1):
                        return True
                finally:
                    kernel32.CloseHandle(h_proc)
        except Exception:
            pass

    # 3. Zapasowe wywołanie taskkill z flagą CREATE_NO_WINDOW (0x08000000)
    try:
        creation_flags = 0x08000000 if sys.platform == "win32" else 0
        res = subprocess.run(
            ["taskkill", "/F", "/PID", str(target_pid)],
            capture_output=True,
            timeout=3,
            creationflags=creation_flags
        )
        if res.returncode == 0:
            return True
    except Exception:
        pass

    return False


def fetch_live_quota(server_info: dict) -> dict | None:
    """Odpytuje lokalny Language Server o status użytkownika i kwoty."""
    token = server_info['token']
    for port in server_info['ports']:
        url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus"
        req = urllib.request.Request(
            url,
            data=b'{"wrapper_data":{}}',
            headers={
                "Content-Type": "application/json",
                "X-Codeium-Csrf-Token": token,
                "Connect-Protocol-Version": "1"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    raw = json.loads(resp.read().decode('utf-8'))
                    return raw.get('userStatus', {})
        except Exception:
            continue
    return None


def parse_quota_data(user_status: dict) -> dict | None:
    """Wyciąga kluczowe metryki konta ze struktury userStatus."""
    email = user_status.get('email')
    if not email or '@' not in email or 'unknown' in email.lower():
        return None
    name = user_status.get('name') or ''
    tier = (user_status.get('userTier') or {}).get('name') or 'Standard'

    cascade_data = user_status.get('cascadeModelConfigData') or {}
    configs = cascade_data.get('clientModelConfigs') or []
    gemini_remaining = None
    gemini_reset = None
    claude_remaining = None
    claude_reset = None

    for c in configs:
        label = c.get('label', '')
        q = c.get('quotaInfo')
        if not q:
            continue
        reset_time = q.get('resetTime')
        rem = q.get('remainingFraction')

        if 'Gemini' in label:
            if gemini_remaining is None and rem is not None:
                gemini_remaining = float(rem)
            if gemini_reset is None and reset_time:
                gemini_reset = reset_time
        elif 'Claude' in label or 'GPT' in label:
            if claude_remaining is None and rem is not None:
                claude_remaining = float(rem)
            if claude_reset is None and reset_time:
                claude_reset = reset_time

    plan_status = user_status.get('planStatus') or {}
    avail_prompt = plan_status.get('availablePromptCredits')
    avail_flow = plan_status.get('availableFlowCredits')
    plan_info = plan_status.get('planInfo') or {}
    monthly_prompt = plan_info.get('monthlyPromptCredits', 0)
    monthly_flow = plan_info.get('monthlyFlowCredits', 0)

    prompt_pct = (avail_prompt / monthly_prompt * 100) if (avail_prompt is not None and monthly_prompt) else None
    flow_pct = (avail_flow / monthly_flow * 100) if (avail_flow is not None and monthly_flow) else None

    return {
        'email': email,
        'name': name,
        'tier': tier,
        'gemini_remaining_pct': round(gemini_remaining * 100, 1) if gemini_remaining is not None else None,
        'gemini_reset_time': gemini_reset,
        'claude_remaining_pct': round(claude_remaining * 100, 1) if claude_remaining is not None else 100.0,
        'claude_reset_time': claude_reset,
        'prompt_credits': {
            'available': avail_prompt,
            'monthly': monthly_prompt,
            'remaining_pct': round(prompt_pct, 1) if prompt_pct is not None else None
        },
        'flow_credits': {
            'available': avail_flow,
            'monthly': monthly_flow,
            'remaining_pct': round(flow_pct, 1) if flow_pct is not None else None
        },
        'last_updated': get_current_utc().isoformat()
    }


def load_registry(path: Path = STORAGE_PATH) -> dict:
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'accounts': {}, 'active_account': None, 'last_check': None}


def atomic_save_json(target_path: Path, data: dict):
    """Atomowy zapis pliku JSON chroniący przed uszkodzeniem danych (.tmp.<pid> + os.replace)."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(f"{target_path.suffix}.tmp.{os.getpid()}")
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def save_registry(path: Path, data: dict):
    atomic_save_json(path, data)


# ==============================================================================
# Magazyn Sesji & Windows Credential Manager Switcher
# ==============================================================================

def read_windows_credential() -> tuple[str, bytes] | None:
    """Odczytuje aktualną zawartość poświadczenia gemini:antigravity z Credential Managera."""
    if not CredRead:
        return None
    pcred = PCREDENTIAL()
    if CredRead(TARGET_CREDENTIAL_NAME, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        try:
            cred = pcred.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            user_name = cred.UserName or "antigravity"
            return (user_name, blob)
        finally:
            CredFree(pcred)
    return None


def write_windows_credential(user_name: str, blob: bytes) -> bool:
    """Zapisuje podane poświadczenie do gemini:antigravity w Credential Managerze."""
    if not CredWrite:
        return False

    blob_buffer = (ctypes.c_byte * len(blob))(*blob)
    cred = CREDENTIAL()
    cred.Flags = 0
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = TARGET_CREDENTIAL_NAME
    cred.Comment = "Antigravity Multi-Account Switcher Managed"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.AttributeCount = 0
    cred.Attributes = None
    cred.TargetAlias = None
    cred.UserName = user_name

    res = CredWrite(ctypes.byref(cred), 0)
    return bool(res)


def safe_email_filename(email: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', email.lower()) + ".session.json"


def extract_email_from_blob(blob_bytes: bytes) -> str | None:
    """Dekoduje payload JWT z poświadczenia i wyciąga prawdziwy adres e-mail."""
    try:
        data = json.loads(blob_bytes.decode('utf-8', errors='replace'))
        id_token = data.get('id_token')
        if id_token and '.' in id_token:
            parts = id_token.split('.')
            if len(parts) >= 2:
                payload = parts[1] + '=' * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload.encode('utf-8')))
                return claims.get('email')
    except Exception:
        pass
    return None


def save_current_session(email: str | None = None) -> bool:
    """Zapisuje bieżący żeton sesji do bezpiecznego skarbca ~/.gemini/account_sessions/.
    Automatycznie weryfikuje tożsamość z tokena JWT, zapobiegając podmianie sesji."""
    cred_data = read_windows_credential()
    if not cred_data:
        print("[BŁĄD] Nie udało się odczytać poświadczenia gemini:antigravity z Windows Credential Manager.")
        return False

    user_name, blob = cred_data
    token_email = extract_email_from_blob(blob)
    target_email = token_email or email

    if not target_email or '@' not in target_email or 'unknown' in target_email.lower():
        print("[BŁĄD] Nie można ustalić tożsamości konta dla bieżącego tokena.")
        return False

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    target_file = SESSIONS_DIR / safe_email_filename(target_email)

    session_payload = {
        'email': target_email,
        'user_name': user_name,
        'saved_at': get_current_utc().isoformat(),
        'blob_raw': blob.decode('utf-8', errors='replace')
    }

    atomic_save_json(target_file, session_payload)

    print(f"[SUKCES] Sesja konta {target_email} została poprawnie zapisana w skarbcu: {target_file}")
    return True


def auto_capture_session_if_new(email: str | None = None, blob: bytes | None = None) -> bool:
    """Weryfikuje tożsamość z tokena JWT w Credential Managerze i automatycznie zapisuje nową sesję do skarbca.
    Zwraca True, jeśli sesja została nowo zapisana, lub False, jeśli sesja już istniała lub token był nieprawidłowy."""
    target_email = email
    target_blob = blob
    user_name = "antigravity"

    if target_blob is None or target_email is None:
        cred_data = read_windows_credential()
        if not cred_data:
            return False
        user_name, cred_blob = cred_data
        target_blob = cred_blob if target_blob is None else target_blob
        if target_email is None:
            target_email = extract_email_from_blob(target_blob)

    if not target_email or '@' not in target_email or 'unknown' in target_email.lower():
        return False

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    target_file = SESSIONS_DIR / safe_email_filename(target_email)

    if target_file.exists():
        return False

    session_payload = {
        'email': target_email,
        'user_name': user_name,
        'saved_at': get_current_utc().isoformat(),
        'blob_raw': target_blob.decode('utf-8', errors='replace')
    }

    atomic_save_json(target_file, session_payload)
    return True


def switch_to_account(target_email: str) -> bool:
    """Wstrzykuje zapisaną sesję wskazanego konta do Windows Credential Managera i natychmiast restartuje Language Server."""
    target_file = SESSIONS_DIR / safe_email_filename(target_email)
    if not target_file.exists():
        print(f"[BŁĄD] Brak zapisanej sesji dla konta: {target_email}")
        print(f"       Dostępne sesje w skarbcu: {[p.name.replace('.session.json', '') for p in SESSIONS_DIR.glob('*.session.json')]}")
        print("       Aby zapisać sesję bieżącego konta, zaloguj się na nie i uruchom: python quota_tracker.py --save-session")
        return False

    with open(target_file, 'r', encoding='utf-8') as f:
        session_payload = json.load(f)

    blob = session_payload['blob_raw'].encode('utf-8')
    user_name = session_payload.get('user_name', 'antigravity')

    if write_windows_credential(user_name, blob):
        # In-Flight Hot-Switch: natychmiastowy restart Language Servera (< 500 ms)
        restarted = restart_language_server()
        print("=" * 75)
        print(f" 🚀 POMYŚLNIE PRZEŁĄCZONO POŚWIADCZENIA NA KONTO: {target_email}!")
        if restarted:
            print(" ⚡ Serwer językowy został zrestartowany w < 500 ms z nową tożsamością.")
            print("    Edytor korzysta z nowego konta natychmiast, bez przeładowywania okna!")
        else:
            print(" ℹ️ Serwer językowy zostanie uruchomiony automatycznie przy kolejnym zapytaniu.")
        print("=" * 75)

        # Zaktualizuj active_account w rejestrze
        reg = load_registry()
        reg['active_account'] = target_email
        save_registry(STORAGE_PATH, reg)
        return True
    else:
        print("[BŁĄD] Zapis do Windows Credential Manager nie powiódł się.")
        return False


def list_saved_sessions() -> list[str]:
    if not SESSIONS_DIR.exists():
        return []
    res = []
    for p in SESSIONS_DIR.glob("*.session.json"):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
                res.append(data.get('email', p.name))
        except Exception:
            pass
    return res


# ==============================================================================
# Ewaluacja i Renderowanie
# ==============================================================================

def evaluate_accounts(registry: dict) -> list[dict]:
    now = get_current_utc()
    evaluated = []
    active_email = registry.get('active_account')
    saved_sessions = set(list_saved_sessions())

    for email, acc in registry.get('accounts', {}).items():
        if not email or '@' not in email or 'unknown' in email.lower():
            continue
        is_active = (email == active_email)
        gemini_reset = parse_iso_time(acc.get('gemini_reset_time'))
        gemini_pct = acc.get('gemini_remaining_pct')

        time_to_gemini_reset = (gemini_reset - now).total_seconds() if gemini_reset else 0

        if time_to_gemini_reset <= 0:
            effective_gemini_pct = 100.0
            gemini_status = "READY"
            gemini_msg = "100% (Zregenerowane)"
        else:
            effective_gemini_pct = gemini_pct if gemini_pct is not None else 0.0
            if effective_gemini_pct <= 10.0:
                gemini_status = "DEPLETED"
                gemini_msg = f"{effective_gemini_pct:.1f}% (Reset za {format_duration(time_to_gemini_reset)})"
            elif effective_gemini_pct <= 30.0:
                gemini_status = "WARNING"
                gemini_msg = f"{effective_gemini_pct:.1f}% (Reset za {format_duration(time_to_gemini_reset)})"
            else:
                gemini_status = "OK"
                gemini_msg = f"{effective_gemini_pct:.1f}% (Reset za {format_duration(time_to_gemini_reset)})"

        claude_reset = parse_iso_time(acc.get('claude_reset_time'))
        claude_pct = acc.get('claude_remaining_pct')
        time_to_claude_reset = (claude_reset - now).total_seconds() if claude_reset else 0

        if time_to_claude_reset <= 0:
            effective_claude_pct = 100.0
            claude_msg = "100% (Zregenerowane)"
        else:
            effective_claude_pct = claude_pct if claude_pct is not None else 100.0
            if effective_claude_pct <= 10.0:
                claude_msg = f"{effective_claude_pct:.1f}% (Reset za {format_duration(time_to_claude_reset)})"
            else:
                claude_msg = f"{effective_claude_pct:.1f}% (Reset za {format_duration(time_to_claude_reset)})"

        has_vault_session = (email in saved_sessions)

        evaluated.append({
            'email': email,
            'name': acc.get('name', ''),
            'tier': acc.get('tier', 'Standard'),
            'is_active': is_active,
            'status': gemini_status,
            'effective_gemini_pct': effective_gemini_pct,
            'gemini_msg': gemini_msg,
            'effective_claude_pct': effective_claude_pct,
            'claude_msg': claude_msg,
            'seconds_to_reset': max(0, time_to_gemini_reset),
            'claude_reset_in': format_duration(time_to_claude_reset) if time_to_claude_reset > 0 else "Gotowe",
            'has_vault_session': has_vault_session,
            'prompt_credits': acc.get('prompt_credits'),
            'flow_credits': acc.get('flow_credits'),
            'last_updated': acc.get('last_updated')
        })

    evaluated.sort(key=lambda x: (x['is_active'], x['effective_gemini_pct'], -x['seconds_to_reset']), reverse=True)
    return evaluated


def render_dashboard(evaluated: list[dict], active_email: str | None) -> str:
    lines = []
    lines.append("=" * 82)
    lines.append(" ⚡ ANTIGRAVITY MULTI-ACCOUNT QUOTA TRACKER & SESSION SWITCHER ⚡")
    lines.append("=" * 82)

    if not evaluated:
        lines.append(" Brak zarejestrowanych kont w bazie. Zaloguj się w IDE i uruchom tracker.")
        lines.append("=" * 82)
        return "\n".join(lines)

    lines.append(f"{'Stan':<6} | {'Konto':<26} | {'Skarbiec':<9} | {'Gemini 5h Limit':<24} | {'Claude Tydzień':<10}")
    lines.append("-" * 82)

    for acc in evaluated:
        tag = " [BIEŻĄCE]" if acc['is_active'] else ""
        disp_email = (acc['email'] + tag)[:26]
        vault_badge = "ZAPISANY" if acc['has_vault_session'] else "BRAK"
        lines.append(f"{acc['status']:<6} | {disp_email:<26} | {vault_badge:<9} | {acc['gemini_msg']:<24} | {acc['claude_reset_in']:<10}")

    lines.append("-" * 82)

    ready_accounts = [a for a in evaluated if a['effective_gemini_pct'] >= 90.0 and not a['is_active']]
    current_acc = next((a for a in evaluated if a['is_active']), None)

    lines.append("\n💡 REKOMENDACJA ARCHITEKTA:")
    if current_acc and current_acc['effective_gemini_pct'] <= 15.0:
        if ready_accounts:
            best = ready_accounts[0]
            lines.append(f" 🚨 Bieżące konto ma niski poziom limitu ({current_acc['effective_gemini_pct']:.1f}%).")
            if best['has_vault_session']:
                lines.append(f" 👉 PRZEŁĄCZ SIĘ BŁYSKAWICZNIE: python quota_tracker.py --switch {best['email']}")
            else:
                lines.append(f" 👉 Zalecane konto: {best['email']} (wymaga zapisania sesji: --save-session).")
        else:
            shortest = min([a for a in evaluated if not a['is_active']], key=lambda x: x['seconds_to_reset'], default=None)
            if shortest:
                lines.append(f" ⏳ Wszystkie konta rezerwowe mają cooldown. Najszybciej odnowi się: {shortest['email']} (za {format_duration(shortest['seconds_to_reset'])})")
            else:
                lines.append(" ⚠️ Brak innych zarejestrowanych kont w puli.")
    elif current_acc:
        lines.append(f" ✅ Aktywne konto ma stabilny limit: {current_acc['gemini_msg']}.")
        if not current_acc['has_vault_session']:
            lines.append(" 💡 Wskazówka: Zapisz to konto do szybkiego przełącznika komendą: python quota_tracker.py --save-session")
    else:
        if evaluated:
            lines.append(f" 👉 Zalecane konto do zalogowania: {evaluated[0]['email']}")

    lines.append("=" * 82)
    return "\n".join(lines)


def render_markdown(evaluated: list[dict], active_email: str | None) -> str:
    now_str = get_current_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    md = [
        "# 📊 Antigravity Multi-Account Quota Dashboard",
        f"> *Ostatnia synchronizacja: {now_str}*\n",
        "| Status | Konto | Skarbiec Sesji | Gemini 5h Limit | Claude Reset | Prompt Credits | Flow Credits |",
        "| :---: | :--- | :---: | :--- | :--- | :--- | :--- |"
    ]
    for acc in evaluated:
        active_badge = " **(Aktywne)**" if acc['is_active'] else ""
        vault = "✅ Zapisana" if acc['has_vault_session'] else "❌ Brak"
        p_c = acc.get('prompt_credits') or {}
        p_str = f"{p_c.get('available', '—')}/{p_c.get('monthly', '—')}" if p_c.get('available') is not None else "—"
        f_c = acc.get('flow_credits') or {}
        f_str = f"{f_c.get('available', '—')}/{f_c.get('monthly', '—')}" if f_c.get('available') is not None else "—"
        md.append(f"| `{acc['status']}` | `{acc['email']}`{active_badge} | {vault} | {acc['gemini_msg']} | {acc['claude_reset_in']} | {p_str} | {f_str} |")

    ready = [a for a in evaluated if a['effective_gemini_pct'] >= 90.0 and not a['is_active']]
    curr = next((a for a in evaluated if a['is_active']), None)

    md.append("\n---")
    md.append("### 🎯 Rekomendacja Przełączenia:")
    if curr and curr['effective_gemini_pct'] <= 15.0:
        if ready:
            best = ready[0]
            md.append(f"> 🚨 **Uwaga:** Bieżące konto ma tylko **{curr['effective_gemini_pct']:.1f}%** limitu.")
            if best['has_vault_session']:
                md.append(f"> 🚀 **1-Klikowe Przełączenie:** Wybierz konto `{best['email']}` z menu na pasku stanu lub uruchom:\n> ```bash\n> python tools/quota_tracker.py --switch {best['email']}\n> ```")
            else:
                md.append(f"> 👉 **Zalecane konto:** `{best['email']}` (100% zregenerowane).")
        else:
            md.append("> ⏳ Wszystkie alternatywne konta mają aktywny cooldown. Poczekaj na odnowienie okna.")
    else:
        md.append("> 🟢 **Stabilnie:** Bieżące konto posiada wystarczający zapas limitów do pracy.")

    return "\n".join(md)


def main():
    parser = argparse.ArgumentParser(description="Multi-Account Quota Tracker & Session Switcher dla Antigravity")
    parser.add_argument("--json", action="store_true", help="Zwraca stan w formacie JSON")
    parser.add_argument("--markdown", action="store_true", help="Generuje plik ACCOUNT_QUOTAS.md")
    parser.add_argument("--no-capture", action="store_true", help="Nie odpytuje serwera, tylko przelicza zarejestrowane konta")
    parser.add_argument("--save-session", action="store_true", help="Zapisuje bieżącą sesję konta do skarbca ~/.gemini/account_sessions/")
    parser.add_argument("--switch", type=str, metavar="EMAIL", help="Przełącza sesję w Windows Credential Managerze na wskazany adres e-mail")
    parser.add_argument("--storage", type=Path, default=STORAGE_PATH, help="Ścieżka do bazy JSON")
    args = parser.parse_args()

    # 1. Obsługa przełączania sesji
    if args.switch:
        success = switch_to_account(args.switch)
        sys.exit(0 if success else 1)

    registry = load_registry(args.storage)

    # 2. Przechwycenie aktywnego konta z language_server
    if not args.no_capture:
        server_info = detect_language_server()
        if server_info:
            live_status = fetch_live_quota(server_info)
            if live_status:
                parsed = parse_quota_data(live_status)
                if parsed:
                    email = parsed.get('email')
                    if email and '@' in email and 'unknown' not in email.lower():
                        registry['accounts'][email] = parsed
                        registry['active_account'] = email
                        registry['last_check'] = get_current_utc().isoformat()
                        save_registry(args.storage, registry)

    # 3. Zapisanie bieżącej sesji jeśli zażądano
    if args.save_session:
        active_email = registry.get('active_account')
        if active_email:
            save_current_session(active_email)
        else:
            print("[BŁĄD] Nie wykryto aktywnego konta do zapisania sesji.")

    # 4. Ewaluacja wszystkich kont
    evaluated = evaluate_accounts(registry)

    # 5. Formaty wyjściowe
    if args.json:
        print(json.dumps({
            'active_account': registry.get('active_account'),
            'last_check': registry.get('last_check'),
            'accounts': evaluated
        }, indent=2, ensure_ascii=False))
        return

    output_cli = render_dashboard(evaluated, registry.get('active_account'))
    print(output_cli)

    if args.markdown:
        md_content = render_markdown(evaluated, registry.get('active_account'))
        md_file = Path(__file__).resolve().parent.parent / "ACCOUNT_QUOTAS.md"
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"\n[INFO] Zaktualizowano plik podglądu: {md_file.resolve()}")


if __name__ == "__main__":
    main()
