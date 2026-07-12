# mic_bridge.py
# Mic Opposite Bridge (VRChat <-> Discord)
# - Mic mute: always opposite between VRChat and Discord (bidirectional)
# - Deafen: optional Discord hotkey + optional VRChat avatar bool param kept in sync (bidirectional)
# - Global action lock: after ANY outbound action, blocks ALL other outbound actions for a shared delay
#   (prevents deafen->mute side effects from causing extra toggles)
# - Pauses mic bridge when Discord is Deafened (but deafen-sync still works)
# - Pauses when Discord/VRChat closed or Discord UIA not ready
#
# Install:
#   py -m pip install psutil pywinauto python-osc zeroconf keyboard
#
# Run without console:
#   rename to mic_bridge.pyw  (recommended)
#   OR run: pythonw mic_bridge.py

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import traceback
import logging
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Tuple, Set, Dict, List

import tkinter as tk
from tkinter import ttk, messagebox

APP_TITLE = "Mic Opposite Bridge (VRChat <-> Discord)"
SETTINGS_PATH = "mic_bridge_settings.json"
LOG_PATH = "mic_bridge.log"
CRASH_PATH = "mic_bridge_crash.txt"
BUILD_ID = "2025-12-29.global-action-lock.all-settings-ui.defaults-deafen-toggle"


# ---------------- Crash helpers ----------------

def _write_crash(text: str) -> None:
    try:
        with open(CRASH_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def _msgbox_error(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def crash_and_exit(where: str, exc: BaseException) -> None:
    tb = f"[{where}] {exc}\n\n{traceback.format_exc()}"
    _write_crash(tb)
    _msgbox_error(APP_TITLE, f"{where} crashed:\n\n{exc}\n\nDetails saved to {CRASH_PATH}")
    raise SystemExit(1)


def _install_thread_excepthook(logger: logging.Logger) -> None:
    def hook(args):
        try:
            msg = (
                f"Thread crash in {args.thread.name}: "
                f"{args.exc_type.__name__}: {args.exc_value}\n"
                f"{''.join(traceback.format_tb(args.exc_traceback))}"
            )
            logger.error(msg)
            _write_crash(msg)
        except Exception:
            pass

    if hasattr(threading, "excepthook"):
        threading.excepthook = hook  # type: ignore[attr-defined]


def now() -> float:
    return time.time()


def normalize(s: str) -> str:
    return (s or "").strip().lower()


def addr_tail(address: str) -> str:
    parts = (address or "").strip().split("/")
    return normalize(parts[-1]) if parts else ""


def find_free_udp(host: str = "127.0.0.1") -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((host, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def find_free_tcp(host: str = "127.0.0.1") -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def parse_csv_list(s: str) -> List[str]:
    if not s.strip():
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


# ---------------- Settings ----------------

@dataclass
class Settings:
    # Core behavior
    system_enabled: bool = False

    # Timing / debouncing
    enforce_interval_ms: int = 60
    ignore_own_change_ms: int = 500  # treat own changes as "not user changes"

    # Command pacing (prevents multi-toggle)
    discord_command_cooldown_ms: int = 300
    vrc_command_cooldown_ms: int = 300

    # Verify delays (give app time to update state before deciding to retry)
    discord_verify_delay_ms: int = 220
    vrc_verify_delay_ms: int = 260

    # NEW: Global shared lock after ANY outbound action (0 = auto)
    global_action_lock_ms: int = 0

    # Retry count
    max_attempts_per_sync: int = 3

    # Process detection (pause bridge when apps closed)
    process_check_interval_ms: int = 900
    discord_process_names: List[str] = None
    vrchat_process_names: List[str] = None

    # Discord
    discord_poll_interval_ms: int = 180
    discord_mute_hotkey: str = "ctrl+shift+f12"
    discord_deafen_hotkey: str = "ctrl+shift+alt+f12"  # default requested
    discord_mute_names: List[str] = None
    discord_deafen_names: List[str] = None

    # UIA scan tuning
    discord_rescan_every_s: float = 6.0
    discord_max_buttons_scan: int = 12000

    # VRChat send
    vrc_send_host: str = "127.0.0.1"
    vrc_send_port: int = 9000  # VRChat listens here by default

    # VRChat receive + OSCQuery default
    vrc_listen_host: str = "127.0.0.1"
    vrc_listen_port: int = 0  # auto
    oscquery_http_port: int = 0  # auto
    oscquery_service_name: str = "MicBridge"

    # VRChat parameters
    vrc_mute_param: str = "MuteSelf"

    # default requested
    vrc_toggle_param: str = "discordtoggle"
    vrc_toggle_param_aliases: List[str] = None

    # NEW: deafen sync param default requested
    vrc_deafen_param: str = "discorddeafen"
    vrc_deafen_param_aliases: List[str] = None

    # VRChat /input/Voice toggle behavior (proven method)
    vrc_press_ms: int = 80
    vrc_extra_release: bool = True
    vrc_extra_release_ms: int = 120

    def __post_init__(self):
        if self.discord_mute_names is None:
            self.discord_mute_names = ["Mute", "Unmute"]
        if self.discord_deafen_names is None:
            self.discord_deafen_names = ["Deafen", "Undeafen"]
        if self.vrc_toggle_param_aliases is None:
            self.vrc_toggle_param_aliases = []
        if self.vrc_deafen_param_aliases is None:
            self.vrc_deafen_param_aliases = []
        if self.discord_process_names is None:
            self.discord_process_names = ["discord.exe"]
        if self.vrchat_process_names is None:
            self.vrchat_process_names = ["vrchat.exe", "vrchat"]


def load_settings(path: str) -> Settings:
    if not os.path.exists(path):
        s = Settings()
        save_settings(path, s)
        return s
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return Settings()
    # keep forwards compatibility with missing keys
    return Settings(**raw)


def save_settings(path: str, s: Settings) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(s), f, indent=2, ensure_ascii=False)


# ---------------- Logging ----------------

class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue[str]):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


def setup_logging(ui_q: queue.Queue[str]) -> logging.Logger:
    logger = logging.getLogger("mic_bridge")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    qh = QueueLogHandler(ui_q)
    qh.setLevel(logging.INFO)
    qh.setFormatter(fmt)
    logger.addHandler(qh)

    logger.info("Build: %s", BUILD_ID)
    logger.info("Logging initialized: %s", LOG_PATH)
    return logger


# ---------------- Shared state ----------------

@dataclass
class SharedState:
    # VRChat
    vrc_muted: Optional[bool] = None
    vrc_last_update_at: float = 0.0
    vrc_running: bool = False

    # VRChat deafen param
    vrc_deafen: Optional[bool] = None
    vrc_deafen_last_update_at: float = 0.0

    # Discord
    discord_muted: Optional[bool] = None
    discord_deafened: Optional[bool] = None
    discord_last_update_at: float = 0.0
    discord_running: bool = False
    discord_ready: bool = False  # UIA can read buttons/state

    # Enable
    system_enabled_ui: bool = False
    system_enabled_vrc: Optional[bool] = None

    # mic-bridge effective (paused by deafen, closed apps, etc)
    mic_effective: bool = False
    pause_reason: str = ""


class StateStore:
    def __init__(self, enabled: bool):
        self._lock = threading.Lock()
        self.state = SharedState(system_enabled_ui=enabled)

        # Global lock shared across ALL outbound actions (mute/deafen/params)
        self._action_lock_until = 0.0
        self._last_action_at = 0.0

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self.state, k, v)

    def snapshot(self) -> SharedState:
        with self._lock:
            return SharedState(**asdict(self.state))

    def is_action_locked(self) -> bool:
        with self._lock:
            return now() < self._action_lock_until

    def last_action_at(self) -> float:
        with self._lock:
            return float(self._last_action_at)

    def lock_actions_for(self, seconds: float) -> None:
        t = now()
        with self._lock:
            self._last_action_at = t
            self._action_lock_until = max(self._action_lock_until, t + max(0.0, seconds))

    def try_begin_action(self, seconds: float) -> bool:
        """Return True if lock acquired, False if currently locked."""
        t = now()
        with self._lock:
            if t < self._action_lock_until:
                return False
            self._last_action_at = t
            self._action_lock_until = t + max(0.0, seconds)
            return True


# ---------------- Process watcher ----------------

def process_running_any(target_names: List[str]) -> bool:
    try:
        import psutil
    except Exception:
        return False

    targets = {normalize(n) for n in (target_names or []) if n}
    if not targets:
        return False

    for p in psutil.process_iter(["name"]):
        try:
            name = normalize(p.info.get("name") or "")
            if name in targets:
                return True
        except Exception:
            continue
    return False


class ProcessWatcher(threading.Thread):
    def __init__(self, settings: Settings, store: StateStore, logger: logging.Logger):
        super().__init__(name="ProcessWatcher", daemon=True)
        self.s = settings
        self.store = store
        self.log = logger
        self.stop_event = threading.Event()
        self._prev_dc = None
        self._prev_vrc = None

    def run(self) -> None:
        self.log.info("ProcessWatcher started.")
        while not self.stop_event.is_set():
            dc = process_running_any(self.s.discord_process_names)
            vrc = process_running_any(self.s.vrchat_process_names)

            if self._prev_dc is None:
                self._prev_dc = dc
            if self._prev_vrc is None:
                self._prev_vrc = vrc

            if dc != self._prev_dc:
                self.log.info("Discord running -> %s", dc)
                self._prev_dc = dc
                if not dc:
                    self.store.update(discord_muted=None, discord_deafened=None, discord_ready=False)

            if vrc != self._prev_vrc:
                self.log.info("VRChat running -> %s", vrc)
                self._prev_vrc = vrc
                if not vrc:
                    self.store.update(
                        vrc_muted=None, vrc_last_update_at=0.0,
                        vrc_deafen=None, vrc_deafen_last_update_at=0.0
                    )

            self.store.update(discord_running=dc, vrc_running=vrc)
            time.sleep(max(0.2, int(self.s.process_check_interval_ms) / 1000.0))

        self.log.info("ProcessWatcher stopped.")


# ---------------- Discord: fast HWND pick + UIA toggle reads ----------------

user32 = ctypes.WinDLL("user32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL


def _hwnd_title(hwnd: int) -> str:
    try:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def _hwnd_area(hwnd: int) -> int:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0
    w = max(0, rect.right - rect.left)
    h = max(0, rect.bottom - rect.top)
    return int(w * h)


class DiscordWindowPicker:
    def __init__(self, logger: logging.Logger):
        self.log = logger

    @staticmethod
    def discord_pids() -> Set[int]:
        try:
            import psutil
        except Exception:
            return set()
        out: Set[int] = set()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if normalize(p.info.get("name") or "") == "discord.exe":
                    out.add(int(p.info["pid"]))
            except Exception:
                continue
        return out

    def pick_main_hwnd(self) -> Optional[Tuple[int, int, str]]:
        pids = self.discord_pids()
        if not pids:
            return None

        best: Dict[str, Any] = {"area": -1, "hwnd": None, "pid": None, "title": ""}

        @EnumWindowsProc
        def cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if int(pid.value) not in pids:
                    return True
                area = _hwnd_area(hwnd)
                if area <= 0:
                    return True
                if area > best["area"]:
                    best["area"] = area
                    best["hwnd"] = int(hwnd)
                    best["pid"] = int(pid.value)
                    best["title"] = _hwnd_title(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(cb, 0)

        if best["hwnd"] is None:
            return None

        self.log.debug(
            "Picked Discord window: hwnd=%s pid=%s title=%r area=%s",
            best["hwnd"], best["pid"], best["title"], best["area"]
        )
        return best["hwnd"], best["pid"], best["title"]


class DiscordUiaReader:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.s = settings
        self.log = logger
        self.picker = DiscordWindowPicker(logger)

        self._root = None
        self._last_attach_at = 0.0
        self._last_scan_at = 0.0

        self._mute_btn = None
        self._deafen_btn = None

        self._mute_targets = set(self.s.discord_mute_names or ["Mute", "Unmute"])
        self._deafen_targets = set(self.s.discord_deafen_names or ["Deafen", "Undeafen"])

        from pywinauto import Desktop
        from pywinauto.timings import Timings
        Timings.window_find_timeout = 3
        Timings.window_find_retry = 0.2
        self._Desktop = Desktop

    @staticmethod
    def _toggle_state(wrapper) -> Optional[int]:
        try:
            return int(wrapper.iface_toggle.CurrentToggleState)
        except Exception:
            return None

    def _attach(self) -> bool:
        picked = self.picker.pick_main_hwnd()
        if not picked:
            self._root = None
            return False
        hwnd, pid, title = picked
        try:
            desk = self._Desktop(backend="uia")
            self._root = desk.window(handle=hwnd).wrapper_object()
            self._last_attach_at = now()
            time.sleep(0.05)
            self.log.debug("UIA attached to Discord hwnd=%s pid=%s title=%r", hwnd, pid, title)
            self._mute_btn = None
            self._deafen_btn = None
            self._last_scan_at = 0.0
            return True
        except Exception as e:
            self.log.debug("UIA attach failed for hwnd=%s: %s", hwnd, e)
            self._root = None
            return False

    def _ensure_attached(self) -> None:
        if self._root is None:
            self._attach()
            return
        if (now() - self._last_attach_at) > 10.0:
            self._attach()

    def _scan_buttons(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            buttons = root.descendants(control_type="Button")
        except Exception as e:
            self.log.debug("descendants(control_type='Button') failed: %s", e)
            return

        mute = None
        deafen = None
        scanned = 0
        max_scan = max(500, int(self.s.discord_max_buttons_scan))

        for b in buttons:
            scanned += 1
            if scanned > max_scan:
                break
            try:
                name = (b.window_text() or "").strip()
                if not name:
                    continue
                try:
                    if hasattr(b, "is_visible") and not b.is_visible():
                        continue
                    if hasattr(b, "is_enabled") and not b.is_enabled():
                        continue
                except Exception:
                    pass

                if mute is None and name in self._mute_targets:
                    if self._toggle_state(b) is not None:
                        mute = b
                if deafen is None and name in self._deafen_targets:
                    if self._toggle_state(b) is not None:
                        deafen = b
                if mute is not None and deafen is not None:
                    break
            except Exception:
                continue

        self._mute_btn = mute
        self._deafen_btn = deafen
        self._last_scan_at = now()
        self.log.debug(
            "Discord button scan: scanned=%s mute_found=%s deafen_found=%s",
            scanned, bool(mute), bool(deafen)
        )

    def _maybe_rescan(self) -> None:
        if (now() - self._last_scan_at) >= float(self.s.discord_rescan_every_s):
            self._scan_buttons()
            return
        if self._mute_btn is None or self._deafen_btn is None:
            self._scan_buttons()

    def read_states(self) -> Tuple[Optional[bool], Optional[bool]]:
        self._ensure_attached()
        if self._root is None:
            return None, None

        self._maybe_rescan()

        muted = None
        deafened = None

        if self._mute_btn is not None:
            ts = self._toggle_state(self._mute_btn)
            if ts is not None:
                muted = (ts == 1)
            else:
                self._mute_btn = None

        if self._deafen_btn is not None:
            ts = self._toggle_state(self._deafen_btn)
            if ts is not None:
                deafened = (ts == 1)
            else:
                self._deafen_btn = None

        if muted is None or deafened is None:
            self._scan_buttons()
            if muted is None and self._mute_btn is not None:
                ts = self._toggle_state(self._mute_btn)
                if ts is not None:
                    muted = (ts == 1)
            if deafened is None and self._deafen_btn is not None:
                ts = self._toggle_state(self._deafen_btn)
                if ts is not None:
                    deafened = (ts == 1)

        return muted, deafened


# ---------------- VRChat OSC + OSCQuery ----------------

class OscQueryHandler(BaseHTTPRequestHandler):
    server_name: str = "MicBridge"
    osc_port: int = 0
    tree: Dict[str, Any] = {}

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path or "/"
        if path.startswith("/?HOST_INFO"):
            self._send_json(
                {
                    "NAME": self.server_name,
                    "OSC_PORT": int(self.osc_port),
                    "OSC_TRANSPORT": "UDP",
                    "EXTENSIONS": {"ACCESS": True, "VALUE": True},
                }
            )
            return
        if path == "/" or path.startswith("/?"):
            self._send_json(self.tree)
            return
        self._send_json({"error": "not found"}, status=404)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class VrchatOsc:
    def __init__(self, settings: Settings, store: StateStore, logger: logging.Logger):
        self.s = settings
        self.store = store
        self.log = logger

        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
        from pythonosc.udp_client import SimpleUDPClient
        from zeroconf import Zeroconf, ServiceInfo

        self.Dispatcher = Dispatcher
        self.ThreadingOSCUDPServer = ThreadingOSCUDPServer
        self.SimpleUDPClient = SimpleUDPClient
        self.Zeroconf = Zeroconf
        self.ServiceInfo = ServiceInfo

        self._osc_server = None
        self._osc_send = self.SimpleUDPClient(self.s.vrc_send_host, int(self.s.vrc_send_port))

        self._http_server = None
        self._zc = None
        self._svc = None

        self._rebuild_alias_sets()

    def _rebuild_alias_sets(self) -> None:
        self._mute_aliases = {normalize(self.s.vrc_mute_param), "muteself"}

        self._toggle_aliases = {normalize(x) for x in (self.s.vrc_toggle_param_aliases or [])}
        if self.s.vrc_toggle_param.strip():
            self._toggle_aliases.add(normalize(self.s.vrc_toggle_param.strip()))

        self._deafen_aliases = {normalize(x) for x in (self.s.vrc_deafen_param_aliases or [])}
        if self.s.vrc_deafen_param.strip():
            self._deafen_aliases.add(normalize(self.s.vrc_deafen_param.strip()))

    def start(self) -> bool:
        host = self.s.vrc_listen_host
        port = int(self.s.vrc_listen_port) or find_free_udp(host)

        disp = self.Dispatcher()
        disp.set_default_handler(self._on_osc)

        try:
            self._osc_server = self.ThreadingOSCUDPServer((host, port), disp)
        except Exception as e:
            self.log.warning("Failed to bind VRChat OSC receive %s:%s: %s", host, port, e)
            return False

        self.s.vrc_listen_port = int(self._osc_server.server_address[1])
        self.log.info("OSC UDP receiver bound on %s:%s", host, self.s.vrc_listen_port)
        threading.Thread(target=self._osc_server.serve_forever, name="VRC_OSC_RX", daemon=True).start()

        http_port = int(self.s.oscquery_http_port) or find_free_tcp(host)
        tree = self._build_oscquery_tree()

        OscQueryHandler.server_name = self.s.oscquery_service_name.strip() or "MicBridge"
        OscQueryHandler.osc_port = int(self.s.vrc_listen_port)
        OscQueryHandler.tree = tree

        self._http_server = ThreadingHTTPServer((host, http_port), OscQueryHandler)
        threading.Thread(target=self._http_server.serve_forever, name="OSCQUERY_HTTP", daemon=True).start()

        self.s.oscquery_http_port = http_port
        self.log.info(
            "OSCQuery HTTP server listening on http://%s:%s (OSC_PORT=%s)",
            host, http_port, self.s.vrc_listen_port
        )

        try:
            self._zc = self.Zeroconf()
            service_type = "_oscjson._tcp.local."
            service_name = OscQueryHandler.server_name or "MicBridge"
            full_name = f"{service_name}.{service_type}"
            addr = socket.inet_aton(host)

            self._svc = self.ServiceInfo(
                type_=service_type,
                name=full_name,
                addresses=[addr],
                port=int(http_port),
                properties={},
                server="localhost.local.",
            )
            self._zc.register_service(self._svc)
            self.log.info("Advertising OSCQuery: %s -> %s:%s", full_name, host, http_port)
        except Exception as e:
            self.log.warning("OSCQuery mDNS advertise failed (bridge may still work): %s", e)

        self.log.info("VRChat OSC sender ready -> %s:%s", self.s.vrc_send_host, self.s.vrc_send_port)
        return True

    def stop(self) -> None:
        try:
            if self._zc and self._svc:
                self._zc.unregister_service(self._svc)
        except Exception:
            pass
        try:
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        self._svc = None
        self._zc = None

        try:
            if self._http_server:
                self._http_server.shutdown()
        except Exception:
            pass
        self._http_server = None

        try:
            if self._osc_server:
                self._osc_server.shutdown()
        except Exception:
            pass
        self._osc_server = None

    def _build_oscquery_tree(self) -> Dict[str, Any]:
        def node(full_path: str, ctype: str = "T", access: int = 3) -> Dict[str, Any]:
            return {"FULL_PATH": full_path, "ACCESS": access, "TYPE": ctype}

        param_contents: Dict[str, Any] = {
            self.s.vrc_mute_param: node(f"/avatar/parameters/{self.s.vrc_mute_param}", "T", 1),
        }

        if self.s.vrc_toggle_param.strip():
            param_contents[self.s.vrc_toggle_param] = node(f"/avatar/parameters/{self.s.vrc_toggle_param}", "T", 3)

        if self.s.vrc_deafen_param.strip():
            param_contents[self.s.vrc_deafen_param] = node(f"/avatar/parameters/{self.s.vrc_deafen_param}", "T", 3)

        contents: Dict[str, Any] = {
            "avatar": {
                "FULL_PATH": "/avatar",
                "CONTENTS": {
                    "parameters": {
                        "FULL_PATH": "/avatar/parameters",
                        "CONTENTS": param_contents,
                    }
                },
            },
            "input": {
                "FULL_PATH": "/input",
                "CONTENTS": {"Voice": node("/input/Voice", "i", 2)},  # int
            },
        }

        return {"FULL_PATH": "/", "CONTENTS": contents}

    def _coerce_bool(self, v: Any) -> Optional[bool]:
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(int(v))
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "on", "yes"):
                return True
            if s in ("0", "false", "off", "no"):
                return False
        return None

    def _on_osc(self, address: str, *args: Any) -> None:
        try:
            tail = addr_tail(address)
            t = now()

            # long or shortened addresses both end in the param name
            if tail in self._mute_aliases:
                v = self._coerce_bool(args[0] if args else None)
                if v is not None:
                    self.store.update(vrc_muted=v, vrc_last_update_at=t)
                return

            if self._toggle_aliases and tail in self._toggle_aliases:
                v = self._coerce_bool(args[0] if args else None)
                if v is not None:
                    self.store.update(system_enabled_vrc=v)
                return

            if self._deafen_aliases and tail in self._deafen_aliases:
                v = self._coerce_bool(args[0] if args else None)
                if v is not None:
                    self.store.update(vrc_deafen=v, vrc_deafen_last_update_at=t)
                return

        except Exception:
            pass

    def toggle_voice_input(self) -> None:
        """Proven /input/Voice int 1 then 0 (+ optional extra 0). Runs async."""
        press_s = max(0.01, int(self.s.vrc_press_ms) / 1000.0)
        extra = bool(self.s.vrc_extra_release)
        extra_s = max(0.01, int(self.s.vrc_extra_release_ms) / 1000.0)

        def run():
            try:
                self.log.info("TX VRChat /input/Voice 1 (int)")
                self._osc_send.send_message("/input/Voice", 1)
                time.sleep(press_s)
                self.log.info("TX VRChat /input/Voice 0 (int)")
                self._osc_send.send_message("/input/Voice", 0)
                if extra:
                    time.sleep(extra_s)
                    self.log.info("TX VRChat /input/Voice 0 (extra release)")
                    self._osc_send.send_message("/input/Voice", 0)
            except Exception as e:
                self.log.warning("VRChat /input/Voice toggle failed: %s", e)

        threading.Thread(target=run, daemon=True).start()

    def ensure_vrc_mute(self, desired_muted: bool, current_muted: Optional[bool]) -> bool:
        desired_muted = bool(desired_muted)
        if current_muted is None:
            self.log.info("VRChat mute unknown -> toggle once, then verify.")
            self.toggle_voice_input()
            return True
        if bool(current_muted) == desired_muted:
            return True
        self.toggle_voice_input()
        return True

    def send_bool_param(self, param_name: str, enabled: bool) -> bool:
        pname = (param_name or "").strip()
        if not pname:
            return False
        try:
            self._osc_send.send_message(f"/avatar/parameters/{pname}", 1 if enabled else 0)
            return True
        except Exception as e:
            self.log.warning("VRChat send param %r failed: %s", pname, e)
            return False

    def send_toggle_param(self, enabled: bool) -> bool:
        return self.send_bool_param(self.s.vrc_toggle_param, enabled)

    def send_deafen_param(self, enabled: bool) -> bool:
        return self.send_bool_param(self.s.vrc_deafen_param, enabled)

    def apply_param_changes(self) -> None:
        """Call after settings changes so alias sets match new param names."""
        self._rebuild_alias_sets()


# ---------------- Discord hotkeys (keyboard) ----------------

def send_hotkey(hotkey: str, logger: logging.Logger) -> bool:
    try:
        import keyboard
    except Exception as e:
        logger.warning("Missing 'keyboard' package: %s", e)
        return False

    hk = (hotkey or "").strip()
    if not hk:
        return False
    try:
        keyboard.press_and_release(hk)
        return True
    except Exception as e:
        logger.warning("keyboard.press_and_release(%r) failed: %s", hk, e)
        return False


# ---------------- Pollers ----------------

class DiscordPoller(threading.Thread):
    def __init__(self, settings: Settings, store: StateStore, reader: DiscordUiaReader, logger: logging.Logger):
        super().__init__(name="DiscordPoller", daemon=True)
        self.s = settings
        self.store = store
        self.reader = reader
        self.log = logger
        self.stop_event = threading.Event()

    def run(self) -> None:
        self.log.info("DiscordPoller started.")
        while not self.stop_event.is_set():
            try:
                muted, deaf = self.reader.read_states()
                ready = (muted is not None and deaf is not None)
                self.store.update(
                    discord_muted=muted,
                    discord_deafened=deaf,
                    discord_last_update_at=now(),
                    discord_ready=ready,
                )
            except Exception as e:
                self.store.update(discord_ready=False)
                self.log.warning("DiscordPoller error: %s\n%s", e, traceback.format_exc())
            time.sleep(max(0.05, int(self.s.discord_poll_interval_ms) / 1000.0))
        self.log.info("DiscordPoller stopped.")


# ---------------- Sync Engine ----------------

class SyncEngine(threading.Thread):
    def __init__(self, settings: Settings, store: StateStore, vrc: VrchatOsc, logger: logging.Logger):
        super().__init__(name="SyncEngine", daemon=True)
        self.s = settings
        self.store = store
        self.vrc = vrc
        self.log = logger
        self.stop_event = threading.Event()

        # last observed
        self.prev_vrc_mute: Optional[bool] = None
        self.prev_dc_mute: Optional[bool] = None
        self.prev_vrc_deafen: Optional[bool] = None
        self.prev_dc_deafen: Optional[bool] = None

        # pending desired targets
        self.pending_vrc_mute: Optional[bool] = None
        self.pending_dc_mute: Optional[bool] = None
        self.pending_vrc_deafen: Optional[bool] = None
        self.pending_dc_deafen: Optional[bool] = None

        self.pending_attempts: Dict[str, int] = {"vrc_mute": 0, "dc_mute": 0, "vrc_def": 0, "dc_def": 0}
        self.pending_next_check: Dict[str, float] = {"vrc_mute": 0.0, "dc_mute": 0.0, "vrc_def": 0.0, "dc_def": 0.0}

    def _auto_lock_seconds(self) -> float:
        if int(self.s.global_action_lock_ms) > 0:
            return max(0.05, int(self.s.global_action_lock_ms) / 1000.0)
        # Auto: cover both cooldown + verify windows (plus a small cushion)
        ms = max(
            int(self.s.discord_command_cooldown_ms),
            int(self.s.vrc_command_cooldown_ms),
            int(self.s.discord_verify_delay_ms),
            int(self.s.vrc_verify_delay_ms),
        ) + 60
        return max(0.08, ms / 1000.0)

    def _send_action_guarded(self, action_name: str, fn) -> bool:
        """Run fn() only if global lock is free; lock globally after success."""
        lock_s = self._auto_lock_seconds()
        if not self.store.try_begin_action(lock_s):
            return False
        ok = False
        try:
            ok = bool(fn())
        except Exception as e:
            self.log.warning("Action %s failed: %s", action_name, e)
            ok = False
        # If action failed, still keep a short lock to avoid rapid-fire retries
        if not ok:
            self.store.lock_actions_for(max(0.08, lock_s * 0.6))
        return ok

    def _can_act(self, st: SharedState) -> bool:
        return bool(st.discord_running and st.vrc_running and st.discord_ready)

    def _tick_pending(self, st: SharedState) -> None:
        t = now()
        max_attempts = max(1, int(self.s.max_attempts_per_sync))

        # If globally locked, do not fire new outbound actions, just wait for states to settle
        if self.store.is_action_locked():
            return

        # VRChat mute pending
        if self.pending_vrc_mute is not None:
            if st.vrc_muted is not None and bool(st.vrc_muted) == bool(self.pending_vrc_mute):
                self.log.info("VRChat reached target mute=%s", self.pending_vrc_mute)
                self.pending_vrc_mute = None
            elif t >= self.pending_next_check["vrc_mute"]:
                if self.pending_attempts["vrc_mute"] >= max_attempts:
                    self.log.warning("VRChat failed to reach mute target after attempts; stopping.")
                    self.pending_vrc_mute = None
                else:
                    self.pending_attempts["vrc_mute"] += 1
                    self.log.info("Set VRChat mute=%s (attempt %s)", self.pending_vrc_mute, self.pending_attempts["vrc_mute"])
                    self._send_action_guarded(
                        "vrc_mute",
                        lambda: self.vrc.ensure_vrc_mute(self.pending_vrc_mute, st.vrc_muted)
                    )
                    self.pending_next_check["vrc_mute"] = t + max(0.08, int(self.s.vrc_verify_delay_ms) / 1000.0)

        # Discord mute pending
        if self.pending_dc_mute is not None:
            if st.discord_muted is not None and bool(st.discord_muted) == bool(self.pending_dc_mute):
                self.log.info("Discord reached target mute=%s", self.pending_dc_mute)
                self.pending_dc_mute = None
            elif t >= self.pending_next_check["dc_mute"]:
                if self.pending_attempts["dc_mute"] >= max_attempts:
                    self.log.warning("Discord failed to reach mute target after attempts; stopping.")
                    self.pending_dc_mute = None
                else:
                    self.pending_attempts["dc_mute"] += 1
                    self.log.info("Set Discord mute=%s (attempt %s)", self.pending_dc_mute, self.pending_attempts["dc_mute"])
                    self._send_action_guarded(
                        "dc_mute_hotkey",
                        lambda: send_hotkey(self.s.discord_mute_hotkey, self.log)
                    )
                    self.pending_next_check["dc_mute"] = t + max(0.08, int(self.s.discord_verify_delay_ms) / 1000.0)

        # VRChat deafen param pending
        if self.pending_vrc_deafen is not None:
            if st.vrc_deafen is not None and bool(st.vrc_deafen) == bool(self.pending_vrc_deafen):
                self.log.info("VRChat reached target deafen_param=%s", self.pending_vrc_deafen)
                self.pending_vrc_deafen = None
            elif t >= self.pending_next_check["vrc_def"]:
                if self.pending_attempts["vrc_def"] >= max_attempts:
                    self.log.warning("VRChat failed to reach deafen-param target after attempts; stopping.")
                    self.pending_vrc_deafen = None
                else:
                    self.pending_attempts["vrc_def"] += 1
                    self.log.info("Set VRChat deafen_param=%s (attempt %s)", self.pending_vrc_deafen, self.pending_attempts["vrc_def"])
                    def send_and_optimistic():
                        ok = self.vrc.send_deafen_param(self.pending_vrc_deafen)
                        if ok:
                            # optimistic update so we don't immediately re-fire if VRChat doesn't echo instantly
                            self.store.update(vrc_deafen=bool(self.pending_vrc_deafen), vrc_deafen_last_update_at=now())
                        return ok
                    self._send_action_guarded("vrc_deafen_param", send_and_optimistic)
                    self.pending_next_check["vrc_def"] = t + max(0.08, int(self.s.vrc_verify_delay_ms) / 1000.0)

        # Discord deafen pending
        if self.pending_dc_deafen is not None:
            if st.discord_deafened is not None and bool(st.discord_deafened) == bool(self.pending_dc_deafen):
                self.log.info("Discord reached target deafen=%s", self.pending_dc_deafen)
                self.pending_dc_deafen = None
            elif t >= self.pending_next_check["dc_def"]:
                if self.pending_attempts["dc_def"] >= max_attempts:
                    self.log.warning("Discord failed to reach deafen target after attempts; stopping.")
                    self.pending_dc_deafen = None
                else:
                    self.pending_attempts["dc_def"] += 1
                    self.log.info("Set Discord deafen=%s (attempt %s)", self.pending_dc_deafen, self.pending_attempts["dc_def"])
                    self._send_action_guarded(
                        "dc_deafen_hotkey",
                        lambda: send_hotkey(self.s.discord_deafen_hotkey, self.log)
                    )
                    self.pending_next_check["dc_def"] = t + max(0.08, int(self.s.discord_verify_delay_ms) / 1000.0)

    def _clear_pending(self) -> None:
        self.pending_vrc_mute = None
        self.pending_dc_mute = None
        self.pending_vrc_deafen = None
        self.pending_dc_deafen = None
        for k in self.pending_attempts:
            self.pending_attempts[k] = 0
            self.pending_next_check[k] = 0.0

    def run(self) -> None:
        self.log.info("SyncEngine started.")
        while not self.stop_event.is_set():
            st = self.store.snapshot()

            # Determine enabled state (VRChat toggle param optional)
            ui_enabled = bool(st.system_enabled_ui)
            vrc_toggle_configured = bool(self.s.vrc_toggle_param.strip())
            vrc_enabled = st.system_enabled_vrc if vrc_toggle_configured else None
            enabled = bool(vrc_enabled) if (vrc_toggle_configured and vrc_enabled is not None) else ui_enabled

            apps_ok = self._can_act(st)

            # Deafen sync enabled only when both configured
            deafen_sync_enabled = bool(self.s.discord_deafen_hotkey.strip()) and bool(self.s.vrc_deafen_param.strip())

            # Compute mic pause reason (mic bridge pauses on deafen)
            pause_reason = ""
            if not st.discord_running:
                pause_reason = "Discord closed"
            elif not st.vrc_running:
                pause_reason = "VRChat closed"
            elif not st.discord_ready:
                pause_reason = "Discord UI not ready"
            elif st.discord_deafened:
                pause_reason = "Discord deafened"

            mic_effective = enabled and (pause_reason == "")
            self.store.update(mic_effective=mic_effective, pause_reason=pause_reason)

            # Tick pending (works for both mute and deafen; global lock prevents cross-triggering)
            if apps_ok:
                self._tick_pending(st)
            else:
                # If apps not OK, stop pending to avoid spam
                self._clear_pending()

            # Ignore window (global) - if any action happened recently, don't treat state changes as user input
            ignore_s = max(0.05, int(self.s.ignore_own_change_ms) / 1000.0)
            action_recent = (now() - self.store.last_action_at()) <= ignore_s

            # ---------------- Deafen sync (always allowed if configured, even when mic bridge paused) ----------------
            if deafen_sync_enabled and apps_ok and not action_recent:
                vrc_def_changed = (st.vrc_deafen is not None and st.vrc_deafen != self.prev_vrc_deafen)
                dc_def_changed = (st.discord_deafened is not None and st.discord_deafened != self.prev_dc_deafen)

                # Only react if no deafen pending active and not globally locked
                if self.pending_vrc_deafen is None and self.pending_dc_deafen is None and not self.store.is_action_locked():
                    if dc_def_changed and st.discord_deafened is not None:
                        # Discord changed -> set VRChat param to match
                        self.pending_vrc_deafen = bool(st.discord_deafened)
                        self.pending_attempts["vrc_def"] = 0
                        self.pending_next_check["vrc_def"] = now()
                    elif vrc_def_changed and st.vrc_deafen is not None:
                        # VRChat param changed -> toggle Discord to match
                        self.pending_dc_deafen = bool(st.vrc_deafen)
                        self.pending_attempts["dc_def"] = 0
                        self.pending_next_check["dc_def"] = now()
                    else:
                        # Enforce match if both known
                        if st.discord_deafened is not None and st.vrc_deafen is not None:
                            if bool(st.discord_deafened) != bool(st.vrc_deafen):
                                self.pending_vrc_deafen = bool(st.discord_deafened)
                                self.pending_attempts["vrc_def"] = 0
                                self.pending_next_check["vrc_def"] = now()

            # ---------------- Mic opposite bridge (only when mic_effective) ----------------
            if mic_effective and apps_ok and not action_recent:
                vrc_changed = (st.vrc_muted is not None and st.vrc_muted != self.prev_vrc_mute)
                dc_changed = (st.discord_muted is not None and st.discord_muted != self.prev_dc_mute)

                if self.pending_vrc_mute is None and self.pending_dc_mute is None and not self.store.is_action_locked():
                    if dc_changed and st.discord_muted is not None:
                        # Discord changed -> VRChat becomes opposite
                        self.pending_vrc_mute = (not bool(st.discord_muted))
                        self.pending_attempts["vrc_mute"] = 0
                        self.pending_next_check["vrc_mute"] = now()
                    elif vrc_changed and st.vrc_muted is not None:
                        # VRChat changed -> Discord becomes opposite
                        self.pending_dc_mute = (not bool(st.vrc_muted))
                        self.pending_attempts["dc_mute"] = 0
                        self.pending_next_check["dc_mute"] = now()
                    else:
                        # Enforce invariant if both known
                        if st.vrc_muted is not None and st.discord_muted is not None:
                            desired_dc = not bool(st.vrc_muted)
                            if bool(st.discord_muted) != bool(desired_dc):
                                self.pending_dc_mute = bool(desired_dc)
                                self.pending_attempts["dc_mute"] = 0
                                self.pending_next_check["dc_mute"] = now()
            else:
                # when paused, don't keep mute pending
                self.pending_vrc_mute = None
                self.pending_dc_mute = None
                self.pending_attempts["vrc_mute"] = 0
                self.pending_attempts["dc_mute"] = 0
                self.pending_next_check["vrc_mute"] = 0.0
                self.pending_next_check["dc_mute"] = 0.0

            # update prev snapshots
            self.prev_vrc_mute = st.vrc_muted
            self.prev_dc_mute = st.discord_muted
            self.prev_vrc_deafen = st.vrc_deafen
            self.prev_dc_deafen = st.discord_deafened

            time.sleep(max(0.02, int(self.s.enforce_interval_ms) / 1000.0))

        self.log.info("SyncEngine stopped.")


# ---------------- Discord UIA polling + window selection helper ----------------

class DiscordUI:
    def __init__(self, settings: Settings, store: StateStore, logger: logging.Logger):
        self.s = settings
        self.store = store
        self.log = logger
        self.reader = DiscordUiaReader(settings, logger)
        self.poller = DiscordPoller(settings, store, self.reader, logger)


# ---------------- UI helpers ----------------

class CollapsibleFrame(ttk.Frame):
    def __init__(self, master, text="Advanced Settings", *args, **kwargs):
        super().__init__(master, *args, **kwargs)
        self._open = tk.BooleanVar(value=False)

        self.btn = ttk.Button(self, text=f"▶ {text}", command=self.toggle)
        self.btn.pack(fill="x")

        self.body = ttk.Frame(self)
        # not packed initially

    def toggle(self):
        if self._open.get():
            self._open.set(False)
            self.body.pack_forget()
            self.btn.configure(text=self.btn.cget("text").replace("▼", "▶"))
        else:
            self._open.set(True)
            if self.btn.cget("text").startswith("▶"):
                self.btn.configure(text=self.btn.cget("text").replace("▶", "▼", 1))
            self.body.pack(fill="both", expand=True, pady=(6, 0))


class ScrollableFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        # mouse wheel
        def _on_mousewheel(event):
            try:
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass

        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)


# ---------------- Tkinter UI ----------------

class App(tk.Tk):
    def __init__(self, settings: Settings):
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(1100, 820)

        self.ui_log_q: queue.Queue[str] = queue.Queue(maxsize=4000)
        self.log = setup_logging(self.ui_log_q)
        _install_thread_excepthook(self.log)

        self.s = settings
        self.store = StateStore(enabled=self.s.system_enabled)

        self.discord = DiscordUI(self.s, self.store, self.log)
        self.vrc = VrchatOsc(self.s, self.store, self.log)

        self.process_watcher: Optional[ProcessWatcher] = None
        self.sync_engine: Optional[SyncEngine] = None

        self._ignore_ui_toggle_until = 0.0

        # Basic UI vars
        self.var_enabled = tk.BooleanVar(value=self.s.system_enabled)
        self.var_mic_eff = tk.StringVar(value="OFF")
        self.var_pause = tk.StringVar(value="")
        self.var_vrc = tk.StringVar(value="Unknown")
        self.var_dc = tk.StringVar(value="Unknown")
        self.var_dc_def = tk.StringVar(value="Unknown")
        self.var_vrc_def = tk.StringVar(value="Unknown")
        self.var_apps = tk.StringVar(value="")

        # Advanced settings UI mapping
        self.adv_vars: Dict[str, Any] = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._start_backend()

        self.after(100, self._pump_logs)
        self.after(150, self._refresh_state_ui)
        self.after(220, self._sync_ui_toggle_from_vrc)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=10)
        top.grid(row=0, column=0, sticky="ew")

        ttk.Checkbutton(top, text="System Enabled (Mic Bridge)", variable=self.var_enabled, command=self._on_toggle).grid(
            row=0, column=0, sticky="w"
        )

        ttk.Label(top, text="Mic Effective:").grid(row=0, column=1, padx=(12, 4), sticky="e")
        ttk.Label(top, textvariable=self.var_mic_eff, width=10).grid(row=0, column=2, sticky="w")

        ttk.Label(top, textvariable=self.var_pause, width=38).grid(row=0, column=3, sticky="w", padx=(10, 0))

        ttk.Button(top, text="Save (from UI)", command=self._save_from_ui).grid(row=0, column=4, padx=(12, 6))
        ttk.Button(top, text="Reload JSON", command=self._reload_json).grid(row=0, column=5, padx=6)
        ttk.Button(top, text="Copy Log Path", command=self._copy_log_path).grid(row=0, column=6, padx=6)

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.grid(row=1, column=0, sticky="nsew")

        left = ttk.Frame(paned, padding=10)
        right = ttk.Frame(paned, padding=10)
        paned.add(left, weight=2)
        paned.add(right, weight=3)

        # Status
        status = ttk.LabelFrame(left, text="Live State", padding=10)
        status.pack(fill="x")

        ttk.Label(status, textvariable=self.var_apps).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(status, text="VRChat mic:").grid(row=1, column=0, sticky="w")
        ttk.Label(status, textvariable=self.var_vrc).grid(row=1, column=1, sticky="w")

        ttk.Label(status, text="Discord mic:").grid(row=2, column=0, sticky="w")
        ttk.Label(status, textvariable=self.var_dc).grid(row=2, column=1, sticky="w")

        ttk.Label(status, text="Discord Deafen:").grid(row=3, column=0, sticky="w")
        ttk.Label(status, textvariable=self.var_dc_def).grid(row=3, column=1, sticky="w")

        ttk.Label(status, text="VRChat Deafen Param:").grid(row=4, column=0, sticky="w")
        ttk.Label(status, textvariable=self.var_vrc_def).grid(row=4, column=1, sticky="w")

        # Quick controls
        quick = ttk.LabelFrame(left, text="Quick Controls", padding=10)
        quick.pack(fill="x", pady=(10, 0))
        ttk.Button(quick, text="Test Discord Mute Hotkey", command=self._test_mute_hotkey).pack(anchor="w")
        ttk.Button(quick, text="Test Discord Deafen Hotkey", command=self._test_deafen_hotkey).pack(anchor="w", pady=(6, 0))
        ttk.Button(quick, text="Restart VRChat OSC/OSCQuery (ports/params)", command=self._restart_vrc).pack(anchor="w", pady=(6, 0))

        # Advanced settings (collapsible)
        adv = CollapsibleFrame(left, text="Advanced Settings (all JSON keys)")
        adv.pack(fill="both", expand=True, pady=(10, 0))

        scroll = ScrollableFrame(adv.body)
        scroll.pack(fill="both", expand=True)

        self._build_all_settings_fields(scroll.inner)

        # Logs
        logs = ttk.LabelFrame(right, text="Logs", padding=10)
        logs.pack(fill="both", expand=True)
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)

        self.txt = tk.Text(logs, wrap="none")
        self.txt.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(logs, orient="vertical", command=self.txt.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=yscroll.set)

    def _build_all_settings_fields(self, parent: ttk.Frame) -> None:
        """
        Every Settings field is editable here.
        Lists are comma-separated.
        """
        row = 0

        def add_label(text: str):
            nonlocal row
            ttk.Label(parent, text=text, font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 4))
            row += 1

        def add_entry(key: str, label: str, width: int = 34):
            nonlocal row
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            var = tk.StringVar(value=str(getattr(self.s, key)))
            ent = ttk.Entry(parent, textvariable=var, width=width)
            ent.grid(row=row, column=1, sticky="ew", pady=2)
            self.adv_vars[key] = ("str", var)
            row += 1

        def add_int(key: str, label: str):
            nonlocal row
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            var = tk.StringVar(value=str(int(getattr(self.s, key))))
            ent = ttk.Entry(parent, textvariable=var, width=18)
            ent.grid(row=row, column=1, sticky="w", pady=2)
            self.adv_vars[key] = ("int", var)
            row += 1

        def add_float(key: str, label: str):
            nonlocal row
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            var = tk.StringVar(value=str(float(getattr(self.s, key))))
            ent = ttk.Entry(parent, textvariable=var, width=18)
            ent.grid(row=row, column=1, sticky="w", pady=2)
            self.adv_vars[key] = ("float", var)
            row += 1

        def add_bool(key: str, label: str):
            nonlocal row
            var = tk.BooleanVar(value=bool(getattr(self.s, key)))
            cb = ttk.Checkbutton(parent, text=label, variable=var)
            cb.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
            self.adv_vars[key] = ("bool", var)
            row += 1

        def add_list(key: str, label: str):
            nonlocal row
            ttk.Label(parent, text=label + " (comma-separated)").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            current = getattr(self.s, key) or []
            var = tk.StringVar(value=", ".join(current))
            ent = ttk.Entry(parent, textvariable=var, width=42)
            ent.grid(row=row, column=1, sticky="ew", pady=2)
            self.adv_vars[key] = ("list", var)
            row += 1

        parent.columnconfigure(1, weight=1)

        add_label("Core")
        add_bool("system_enabled", "system_enabled (default UI toggle)")
        add_int("enforce_interval_ms", "enforce_interval_ms")
        add_int("ignore_own_change_ms", "ignore_own_change_ms")
        add_int("max_attempts_per_sync", "max_attempts_per_sync")
        add_int("global_action_lock_ms", "global_action_lock_ms (0 = auto)")

        add_label("Action pacing")
        add_int("discord_command_cooldown_ms", "discord_command_cooldown_ms")
        add_int("vrc_command_cooldown_ms", "vrc_command_cooldown_ms")
        add_int("discord_verify_delay_ms", "discord_verify_delay_ms")
        add_int("vrc_verify_delay_ms", "vrc_verify_delay_ms")

        add_label("Process detection")
        add_int("process_check_interval_ms", "process_check_interval_ms")
        add_list("discord_process_names", "discord_process_names")
        add_list("vrchat_process_names", "vrchat_process_names")

        add_label("Discord")
        add_int("discord_poll_interval_ms", "discord_poll_interval_ms")
        add_entry("discord_mute_hotkey", "discord_mute_hotkey")
        add_entry("discord_deafen_hotkey", "discord_deafen_hotkey")
        add_list("discord_mute_names", "discord_mute_names")
        add_list("discord_deafen_names", "discord_deafen_names")
        add_float("discord_rescan_every_s", "discord_rescan_every_s")
        add_int("discord_max_buttons_scan", "discord_max_buttons_scan")

        add_label("VRChat networking")
        add_entry("vrc_send_host", "vrc_send_host")
        add_int("vrc_send_port", "vrc_send_port")
        add_entry("vrc_listen_host", "vrc_listen_host")
        add_int("vrc_listen_port", "vrc_listen_port (0 = auto)")
        add_int("oscquery_http_port", "oscquery_http_port (0 = auto)")
        add_entry("oscquery_service_name", "oscquery_service_name")

        add_label("VRChat parameters")
        add_entry("vrc_mute_param", "vrc_mute_param")
        add_entry("vrc_toggle_param", "vrc_toggle_param (optional)")
        add_list("vrc_toggle_param_aliases", "vrc_toggle_param_aliases")
        add_entry("vrc_deafen_param", "vrc_deafen_param (optional)")
        add_list("vrc_deafen_param_aliases", "vrc_deafen_param_aliases")

        add_label("VRChat /input/Voice toggle")
        add_int("vrc_press_ms", "vrc_press_ms")
        add_bool("vrc_extra_release", "vrc_extra_release")
        add_int("vrc_extra_release_ms", "vrc_extra_release_ms")

    def _apply_adv_to_settings(self) -> None:
        # apply from adv_vars into self.s with type conversion + validation
        for key, (kind, var) in self.adv_vars.items():
            try:
                if kind == "bool":
                    setattr(self.s, key, bool(var.get()))
                elif kind == "int":
                    setattr(self.s, key, int(str(var.get()).strip()))
                elif kind == "float":
                    setattr(self.s, key, float(str(var.get()).strip()))
                elif kind == "list":
                    setattr(self.s, key, parse_csv_list(str(var.get())))
                else:
                    setattr(self.s, key, str(var.get()))
            except Exception as e:
                raise ValueError(f"Bad value for {key}: {var.get()} ({e})")

        # also sync the main UI toggle value
        self.s.system_enabled = bool(self.var_enabled.get())
        self.store.update(system_enabled_ui=self.s.system_enabled)

        # apply param alias sets
        self.vrc.apply_param_changes()

    def _reload_json(self) -> None:
        try:
            s2 = load_settings(SETTINGS_PATH)
            self.s = s2
            # rebuild advanced vars UI values
            for key, (kind, var) in self.adv_vars.items():
                if kind == "bool":
                    var.set(bool(getattr(self.s, key)))
                elif kind == "int":
                    var.set(str(int(getattr(self.s, key))))
                elif kind == "float":
                    var.set(str(float(getattr(self.s, key))))
                elif kind == "list":
                    var.set(", ".join(getattr(self.s, key) or []))
                else:
                    var.set(str(getattr(self.s, key)))
            self.var_enabled.set(bool(self.s.system_enabled))
            self.vrc.apply_param_changes()
            messagebox.showinfo(APP_TITLE, "Reloaded settings from JSON.")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Failed to reload JSON:\n\n{e}")

    def _save_from_ui(self) -> None:
        try:
            self._apply_adv_to_settings()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Fix settings:\n\n{e}")
            return
        try:
            save_settings(SETTINGS_PATH, self.s)
            messagebox.showinfo(APP_TITLE, f"Saved {SETTINGS_PATH}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Failed saving JSON:\n\n{e}")

    def _copy_log_path(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(LOG_PATH)
        messagebox.showinfo(APP_TITLE, f"Copied log path:\n{LOG_PATH}")

    def _start_backend(self) -> None:
        self.log.info("Starting backend...")
        self.store.update(system_enabled_ui=bool(self.var_enabled.get()))

        self.process_watcher = ProcessWatcher(self.s, self.store, self.log)
        self.process_watcher.start()

        ok = self.vrc.start()
        if not ok:
            self.log.warning("VRChat OSC start failed (Discord side will still run).")

        self.discord.poller.start()

        self.sync_engine = SyncEngine(self.s, self.store, self.vrc, self.log)
        self.sync_engine.start()

        self.log.info("Backend running.")

    def _stop_backend(self) -> None:
        try:
            if self.process_watcher:
                self.process_watcher.stop_event.set()
            if self.discord.poller:
                self.discord.poller.stop_event.set()
            if self.sync_engine:
                self.sync_engine.stop_event.set()
        except Exception:
            pass
        time.sleep(0.25)
        try:
            self.vrc.stop()
        except Exception:
            pass

    def _restart_vrc(self) -> None:
        try:
            self.vrc.stop()
        except Exception:
            pass
        time.sleep(0.2)
        # re-open with possibly new ports/settings
        try:
            self.vrc = VrchatOsc(self.s, self.store, self.log)
            ok = self.vrc.start()
            if not ok:
                messagebox.showwarning(APP_TITLE, "Restarted VRChat OSC, but bind failed (check ports).")
            else:
                messagebox.showinfo(APP_TITLE, "Restarted VRChat OSC/OSCQuery.")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Failed restarting VRChat OSC:\n\n{e}")

    def _on_toggle(self) -> None:
        if now() < self._ignore_ui_toggle_until:
            return
        enabled = bool(self.var_enabled.get())
        self.store.update(system_enabled_ui=enabled)
        self.s.system_enabled = enabled

        if self.s.vrc_toggle_param.strip():
            # send via global lock so it doesn't collide with other actions
            lock_s = max(0.08, SyncEngine(self.s, self.store, self.vrc, self.log)._auto_lock_seconds())
            if self.store.try_begin_action(lock_s):
                self.vrc.send_toggle_param(enabled)

    def _sync_ui_toggle_from_vrc(self) -> None:
        try:
            st = self.store.snapshot()
            if self.s.vrc_toggle_param.strip() and st.system_enabled_vrc is not None:
                v = bool(st.system_enabled_vrc)
                if bool(self.var_enabled.get()) != v:
                    self._ignore_ui_toggle_until = now() + 0.35
                    self.var_enabled.set(v)
                    self.store.update(system_enabled_ui=v)
        except Exception:
            pass
        self.after(220, self._sync_ui_toggle_from_vrc)

    def _test_mute_hotkey(self) -> None:
        hk = self.s.discord_mute_hotkey
        ok = send_hotkey(hk, self.log)
        messagebox.showinfo(APP_TITLE, f"Sent MUTE hotkey: {'OK' if ok else 'FAILED'}\n\n{hk}")

    def _test_deafen_hotkey(self) -> None:
        hk = (self.s.discord_deafen_hotkey or "").strip()
        if not hk:
            messagebox.showwarning(APP_TITLE, "discord_deafen_hotkey is blank.")
            return
        ok = send_hotkey(hk, self.log)
        messagebox.showinfo(APP_TITLE, f"Sent DEAFEN hotkey: {'OK' if ok else 'FAILED'}\n\n{hk}")

    def _pump_logs(self) -> None:
        try:
            while True:
                line = self.ui_log_q.get_nowait()
                self.txt.insert("end", line + "\n")
                self.txt.see("end")
                if int(float(self.txt.index("end"))) > 1600:
                    self.txt.delete("1.0", "450.0")
        except queue.Empty:
            pass
        self.after(120, self._pump_logs)

    def _refresh_state_ui(self) -> None:
        st = self.store.snapshot()

        self.var_vrc.set("Muted" if st.vrc_muted else ("Unmuted" if st.vrc_muted is False else "Unknown"))
        self.var_dc.set("Muted" if st.discord_muted else ("Unmuted" if st.discord_muted is False else "Unknown"))
        self.var_dc_def.set("Deafened" if st.discord_deafened else ("Undeafened" if st.discord_deafened is False else "Unknown"))

        if self.s.vrc_deafen_param.strip():
            self.var_vrc_def.set("True" if st.vrc_deafen else ("False" if st.vrc_deafen is False else "Unknown"))
        else:
            self.var_vrc_def.set("(not set)")

        apps = []
        apps.append(f"VRChat: {'RUNNING' if st.vrc_running else 'CLOSED'}")
        apps.append(f"Discord: {'RUNNING' if st.discord_running else 'CLOSED'}")
        if st.discord_running:
            apps.append(f"Discord UI: {'READY' if st.discord_ready else 'NOT READY'}")
        apps.append(f"GlobalLock: {'ON' if self.store.is_action_locked() else 'OFF'}")
        self.var_apps.set(" | ".join(apps))

        self.var_mic_eff.set("ON" if st.mic_effective else "OFF")
        self.var_pause.set(f"PAUSED ({st.pause_reason})" if st.pause_reason else "")

        self.after(150, self._refresh_state_ui)

    def _on_close(self) -> None:
        self._stop_backend()
        self.destroy()


def main() -> None:
    s = load_settings(SETTINGS_PATH)
    app = App(s)
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        crash_and_exit("Main", e)
