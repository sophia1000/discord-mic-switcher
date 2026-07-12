"""Discord / VRChat opposite-mute bridge.

Discord is intentionally monitored and controlled with its configured global
keybind.  No Discord API permissions or token are required.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import ctypes
import importlib.util
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk
from typing import Any, Callable, Optional

try:
    import keyboard
except ImportError:  # Allows the state machine to be imported and tested.
    keyboard = None

try:
    from pythonosc import dispatcher, osc_server, udp_client
except ImportError:
    dispatcher = osc_server = udp_client = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "mic_sync_config.json")
LOG_FILE = os.path.join(BASE_DIR, "mic_sync.log")

MODE_VRC_MASTER = "vrchat_master"
MODE_DISCORD_MASTER = "discord_master"
MODE_DYNAMIC = "dynamic"
VALID_MODES = {MODE_VRC_MASTER, MODE_DISCORD_MASTER, MODE_DYNAMIC}

DEFAULT_CONFIG = {
    "discord_mute_hotkey": "ctrl+shift+m",
    "discord_deafen_hotkey": "ctrl+shift+alt+f12",
    "discord_poll_interval_ms": 100,
    "discord_rescan_every_s": 6.0,
    "discord_max_buttons_scan": 12000,
    "discord_mute_names": ["Mute", "Unmute"],
    "discord_deafen_names": ["Deafen", "Undeafen"],
    "vrchat_osc_send_port": 9000,
    "vrchat_osc_receive_port": 9001,
    "vrchat_osc_ip": "127.0.0.1",
    "toggle_parameter_name": "ToggleMicSync",
    "mute_parameter_name": "MuteSelf",
    "sync_mode": MODE_DYNAMIC,
    "deafen_sync_enabled": False,
    "deafen_parameter_name": "discorddeafen",
    "deafen_sync_mode": MODE_DYNAMIC,
    "system_enabled": True,
    "logging_enabled": False,
}


class ConfigManager:
    def __init__(self, path: str):
        self.path = path
        self.config: dict[str, Any] = {}
        self.last_modified = 0.0
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            self.config = {**DEFAULT_CONFIG, **loaded}
        except (OSError, ValueError):
            self.config = DEFAULT_CONFIG.copy()
        if self.config.get("sync_mode") not in VALID_MODES:
            self.config["sync_mode"] = MODE_DYNAMIC
        if self.config.get("deafen_sync_mode") not in VALID_MODES:
            self.config["deafen_sync_mode"] = MODE_DYNAMIC
        self.save()

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle, indent=4)
        self.last_modified = os.path.getmtime(self.path)

    def update(self, key: str, value: Any) -> None:
        if self.config.get(key) == value:
            return
        self.config[key] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def changed_on_disk(self) -> bool:
        try:
            modified = os.path.getmtime(self.path)
        except OSError:
            return False
        if modified == self.last_modified:
            return False
        self.load()
        return True


class MicSyncLogger:
    def __init__(self, path: str):
        self.path = path
        self.logger = logging.getLogger(f"MicBridge-{id(self)}")
        self.logger.setLevel(logging.DEBUG)
        self.handler: Optional[logging.Handler] = None

    @property
    def enabled(self) -> bool:
        return self.handler is not None

    def enable(self) -> None:
        if self.handler:
            return
        handler = logging.FileHandler(self.path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        self.logger.addHandler(handler)
        self.handler = handler
        self.log("info", "Mic Bridge logging started")

    def disable(self) -> None:
        if not self.handler:
            return
        self.log("info", "Mic Bridge logging stopped")
        self.logger.removeHandler(self.handler)
        self.handler.close()
        self.handler = None

    def log(self, level: str, message: str) -> None:
        if self.handler:
            getattr(self.logger, level, self.logger.info)(message)


StateCallback = Callable[[str, bool, bool], None]


class DiscordWindowPicker:
    """Finds Discord's largest visible top-level window."""

    def __init__(self, logger: MicSyncLogger):
        self.logger = logger

    @staticmethod
    def _discord_pids() -> set[int]:
        try:
            import psutil
        except ImportError:
            return set()
        result = set()
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if (process.info.get("name") or "").lower() == "discord.exe":
                    result.add(int(process.info["pid"]))
            except Exception:
                continue
        return result

    def pick(self) -> Optional[int]:
        if os.name != "nt":
            return None
        pids = self._discord_pids()
        if not pids:
            return None
        user32 = ctypes.windll.user32
        best = {"hwnd": None, "area": 0}
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def visit(hwnd: int, _lparam: int) -> bool:
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if int(pid.value) not in pids:
                    return True
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                if area > best["area"]:
                    best.update(hwnd=int(hwnd), area=area)
            except Exception:
                pass
            return True

        user32.EnumWindows(visit, 0)
        return best["hwnd"]


class DiscordUiaReader:
    """Reads Discord's accessibility toggle buttons like a screen reader."""

    def __init__(self, config: ConfigManager, logger: MicSyncLogger):
        from pywinauto import Desktop
        from pywinauto.timings import Timings

        Timings.window_find_timeout = 3
        Timings.window_find_retry = 0.2
        self._Desktop = Desktop
        self.config = config
        self.logger = logger
        self.picker = DiscordWindowPicker(logger)
        self._root = None
        self._hwnd: Optional[int] = None
        self._attached_at = 0.0
        self._scanned_at = 0.0
        self._mute_button = None
        self._deafen_button = None

    @staticmethod
    def _toggle_state(button: Any) -> Optional[int]:
        try:
            return int(button.iface_toggle.CurrentToggleState)
        except Exception:
            return None

    def _attach(self) -> bool:
        hwnd = self.picker.pick()
        if hwnd is None:
            self._root = None
            self._hwnd = None
            return False
        try:
            self._root = self._Desktop(backend="uia").window(handle=hwnd).wrapper_object()
            self._hwnd = hwnd
            self._attached_at = time.monotonic()
            self._scanned_at = 0.0
            self._mute_button = self._deafen_button = None
            # Discord's UIA tree can lag briefly behind the HWND attachment.
            time.sleep(0.05)
            return True
        except Exception as exc:
            self.logger.log("debug", f"Discord UI Automation attach failed: {exc}")
            self._root = None
            self._hwnd = None
            return False

    def is_hung(self) -> bool:
        if self._hwnd is None or os.name != "nt":
            return False
        try:
            return bool(ctypes.windll.user32.IsHungAppWindow(self._hwnd))
        except Exception:
            return False

    def _ensure_attached(self) -> None:
        if self._root is None or time.monotonic() - self._attached_at > 10.0:
            self._attach()

    def _scan(self) -> None:
        if self._root is None:
            return
        try:
            buttons = self._root.descendants(control_type="Button")
        except Exception as exc:
            self.logger.log("debug", f"Discord accessibility scan failed: {exc}")
            self._root = None
            return
        mute_names = set(self.config.get("discord_mute_names", ["Mute", "Unmute"]))
        deafen_names = set(self.config.get("discord_deafen_names", ["Deafen", "Undeafen"]))
        max_buttons = max(500, int(self.config.get("discord_max_buttons_scan", 12000)))
        mute = deafen = None
        for index, button in enumerate(buttons):
            if index >= max_buttons:
                break
            try:
                name = (button.window_text() or "").strip()
                if not name or not button.is_visible() or not button.is_enabled():
                    continue
                if mute is None and name in mute_names and self._toggle_state(button) is not None:
                    mute = button
                if deafen is None and name in deafen_names and self._toggle_state(button) is not None:
                    deafen = button
                if mute is not None and deafen is not None:
                    break
            except Exception:
                continue
        self._mute_button = mute
        self._deafen_button = deafen
        self._scanned_at = time.monotonic()
        self.logger.log("debug", f"Discord UI buttons: mute={bool(mute)}, deafen={bool(deafen)}")

    def read_states(self) -> tuple[Optional[bool], Optional[bool]]:
        self._ensure_attached()
        if self._root is None or self.is_hung():
            return None, None
        rescan_after = float(self.config.get("discord_rescan_every_s", 6.0))
        if (self._mute_button is None or self._deafen_button is None
                or time.monotonic() - self._scanned_at >= rescan_after):
            self._scan()

        def read(button: Any) -> Optional[bool]:
            state = self._toggle_state(button) if button is not None else None
            return state == 1 if state is not None else None

        muted = read(self._mute_button)
        deafened = read(self._deafen_button)
        if muted is None or deafened is None:
            self._scan()
            muted = read(self._mute_button)
            deafened = read(self._deafen_button)
        return muted, deafened


class DiscordHandler:
    """Reads Discord through UI Automation and controls it with hotkeys."""

    def __init__(self, hotkey: str, logger: MicSyncLogger, config: ConfigManager):
        self.hotkey = hotkey
        self.deafen_hotkey = config.get("discord_deafen_hotkey")
        self.logger = logger
        self.config = config
        self.is_muted: Optional[bool] = None
        self.is_deafened: Optional[bool] = None
        self.connected = False
        self.reader_ready = False
        self.commands_suspended = False
        self.callback: Optional[StateCallback] = None
        self._lock = threading.RLock()
        self._reader: Optional[DiscordUiaReader] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._raw_mute: Optional[bool] = None
        self._raw_deafen: Optional[bool] = None
        self._mute_streak = self._deafen_streak = 0
        self._expected_value: Optional[bool] = None
        self._expected_deadline = 0.0
        self._expected_deafen: Optional[bool] = None
        self._expected_deafen_deadline = 0.0

    def set_callback(self, callback: StateCallback) -> None:
        self.callback = callback

    def connect(self) -> bool:
        if keyboard is None:
            self.logger.log("error", "The 'keyboard' package is not installed")
            return False
        if self.connected:
            return True
        try:
            if importlib.util.find_spec("pywinauto") is None or importlib.util.find_spec("psutil") is None:
                raise ImportError("pywinauto and psutil are required")
            self._stop_event.clear()
            self.connected = True
            self._poll_thread = threading.Thread(target=self._poll_loop, name="DiscordUIA", daemon=True)
            self._poll_thread.start()
            self.logger.log("info", "Discord screen-reader/UI Automation monitor started")
            return True
        except Exception as exc:
            self.logger.log("error", f"Discord UI Automation monitor failed: {exc}")
            return False

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._poll_thread and self._poll_thread is not threading.current_thread():
            self._poll_thread.join(timeout=1.0)
        self._poll_thread = None
        self._reader = None
        self.connected = False
        self.reader_ready = False

    def change_hotkeys(self, hotkey: str, deafen_hotkey: str) -> None:
        self.hotkey = hotkey
        self.deafen_hotkey = deafen_hotkey

    @staticmethod
    def _debounce(raw: Optional[bool], previous: Optional[bool], streak: int) -> tuple[Optional[bool], int]:
        if raw is None:
            return previous, 0
        if previous is None or raw != previous:
            return raw, 1
        return previous, streak + 1

    def _poll_loop(self) -> None:
        # UI Automation is COM-based. Construct and use the reader on this
        # same thread so its interfaces never cross COM apartments.
        try:
            self._reader = DiscordUiaReader(self.config, self.logger)
        except Exception as exc:
            self.connected = False
            self.reader_ready = False
            self.logger.log("error", f"Discord UI Automation initialization failed: {exc}")
            return
        while not self._stop_event.is_set():
            try:
                assert self._reader is not None
                mute, deafen = self._reader.read_states()
                self._raw_mute, self._mute_streak = self._debounce(
                    mute, self._raw_mute, self._mute_streak)
                self._raw_deafen, self._deafen_streak = self._debounce(
                    deafen, self._raw_deafen, self._deafen_streak)
                self.reader_ready = mute is not None and deafen is not None
                stable_mute = self._raw_mute if self._mute_streak >= 3 else None
                stable_deafen = self._raw_deafen if self._deafen_streak >= 3 else None
                self._accept_states(stable_mute, stable_deafen)
            except Exception as exc:
                self.reader_ready = False
                self.logger.log("debug", f"Discord UI Automation read failed: {exc}")
            interval = max(50, int(self.config.get("discord_poll_interval_ms", 100))) / 1000.0
            self._stop_event.wait(interval)

    def _accept_states(self, muted: Optional[bool], deafened: Optional[bool]) -> None:
        events: list[tuple[str, bool, bool]] = []
        with self._lock:
            if deafened is not None and deafened != self.is_deafened:
                initial = self.is_deafened is None
                self.is_deafened = deafened
                expected = (
                    self._expected_deafen == deafened
                    and time.monotonic() <= self._expected_deafen_deadline
                )
                self._expected_deafen = None
                self._expected_deafen_deadline = 0.0
                if not expected:
                    events.append(("discord_deafen", deafened, initial))
                else:
                    self.logger.log("debug", f"Consumed expected Discord deafen state -> {deafened}")
            if muted is not None and muted != self.is_muted:
                initial = self.is_muted is None
                self.is_muted = muted
                expected = (
                    self._expected_value == muted
                    and time.monotonic() <= self._expected_deadline
                )
                self._expected_value = None
                self._expected_deadline = 0.0
                if not expected:
                    events.append(("discord", muted, initial))
                else:
                    self.logger.log("debug", f"Consumed expected Discord UI state -> {muted}")
        for source, value, initial in events:
            self.logger.log("info", f"Discord {source} changed -> {value}")
            if self.callback:
                self.callback(source, value, initial)

    def set_mute(self, muted: bool) -> bool:
        with self._lock:
            if self.commands_suspended:
                return False
            if self.is_muted is None or not self.reader_ready:
                return False
            if self.is_muted == muted:
                return False
            if keyboard is None or not self.connected or not self.hotkey:
                return False
            if self._expected_value == muted and time.monotonic() <= self._expected_deadline:
                return False
            try:
                keyboard.send(self.hotkey)
            except Exception as exc:
                self.logger.log("error", f"Could not send Discord keybind: {exc}")
                return False
            # UI Automation remains the authority. It will confirm the state.
            self._expected_value = muted
            self._expected_deadline = time.monotonic() + 2.0
        self.logger.log("info", f"Discord set by bridge -> {muted}")
        return True

    def set_deafen(self, deafened: bool) -> bool:
        with self._lock:
            if self.commands_suspended:
                return False
            if self.is_deafened is None or not self.reader_ready:
                return False
            if self.is_deafened == deafened:
                return False
            if keyboard is None or not self.connected or not self.deafen_hotkey:
                return False
            if (self._expected_deafen == deafened
                    and time.monotonic() <= self._expected_deafen_deadline):
                return False
            try:
                keyboard.send(self.deafen_hotkey)
            except Exception as exc:
                self.logger.log("error", f"Could not send Discord deafen keybind: {exc}")
                return False
            self._expected_deafen = deafened
            self._expected_deafen_deadline = time.monotonic() + 2.0
        self.logger.log("info", f"Discord deafen set by bridge -> {deafened}")
        return True


class VRChatOSCHandler:
    def __init__(self, ip: str, send_port: int, receive_port: int, logger: MicSyncLogger):
        self.ip = ip
        self.send_port = send_port
        self.receive_port = receive_port
        self.logger = logger
        self.mute_param_name = "MuteSelf"
        self.toggle_param_name = "ToggleMicSync"
        self.deafen_param_name = "discorddeafen"
        self.client = None
        self.server = None
        self.server_thread: Optional[threading.Thread] = None
        self.callback: Optional[StateCallback] = None
        self.is_muted = False
        self.mute_param_found = False
        self.deafen_param_found = False
        self.is_deafened: Optional[bool] = None
        self.toggle_param_found = False
        self.toggle_enabled = True
        self._last_toggle: Optional[bool] = None
        self._expected_value: Optional[bool] = None
        self._expected_deadline = 0.0
        self._expected_deafen: Optional[bool] = None
        self._expected_deafen_deadline = 0.0
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self.client is not None and self.server is not None

    def set_callback(self, callback: StateCallback) -> None:
        self.callback = callback

    def set_parameter_names(self, mute_name: str, toggle_name: str, deafen_name: str) -> None:
        if deafen_name != self.deafen_param_name:
            self.deafen_param_found = False
            self.is_deafened = None
        self.mute_param_name = mute_name
        self.toggle_param_name = toggle_name
        self.deafen_param_name = deafen_name

    def connect(self) -> bool:
        if self.connected:
            return True
        if dispatcher is None or osc_server is None or udp_client is None:
            self.logger.log("error", "The 'python-osc' package is not installed")
            return False
        try:
            self.client = udp_client.SimpleUDPClient(self.ip, self.send_port)
            routes = dispatcher.Dispatcher()
            routes.map("/avatar/parameters/*", self._handle_parameter)
            self.server = osc_server.ThreadingOSCUDPServer(("127.0.0.1", self.receive_port), routes)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            self.logger.log("info", f"VRChat OSC ready: send {self.send_port}, receive {self.receive_port}")
            return True
        except Exception as exc:
            self.client = self.server = None
            self.logger.log("error", f"VRChat OSC failed: {exc}")
            return False

    def disconnect(self) -> None:
        server, self.server = self.server, None
        self.client = None
        if server:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        self.mute_param_found = False
        self.deafen_param_found = False
        self.is_deafened = None

    def restart(self, send_port: int, receive_port: int) -> None:
        self.disconnect()
        self.send_port = send_port
        self.receive_port = receive_port
        self.connect()

    def _handle_parameter(self, address: str, *args: Any) -> None:
        if not args:
            return
        name = address.rsplit("/", 1)[-1]
        value = bool(args[0])
        if name == self.mute_param_name:
            self._handle_mute(value)
        elif self.deafen_param_name and name == self.deafen_param_name:
            self._handle_deafen(value)
        elif name == self.toggle_param_name:
            initial = self._last_toggle is None
            changed = initial or value != self._last_toggle
            self._last_toggle = value
            self.toggle_enabled = value
            self.toggle_param_found = True
            if changed and not initial and self.callback:
                self.callback("toggle", value, False)

    def _handle_mute(self, value: bool) -> None:
        now = time.monotonic()
        with self._lock:
            initial = not self.mute_param_found
            previous = self.is_muted
            self.mute_param_found = True
            self.is_muted = value
            expected = (
                self._expected_value is not None
                and now <= self._expected_deadline
                and value == self._expected_value
            )
            if expected:
                self._expected_value = None
                self._expected_deadline = 0.0
            elif self._expected_value is not None:
                # A different value is a real user change, even if it raced
                # with our write. Do not let a later stale echo hide it.
                self._expected_value = None
                self._expected_deadline = 0.0
            changed = initial or value != previous
        if expected:
            self.logger.log("debug", f"Consumed expected VRChat OSC echo -> {value}")
            return
        if changed:
            self.logger.log("info", f"VRChat changed -> {value}")
            if self.callback:
                self.callback("vrchat", value, initial)

    def set_mute(self, muted: bool) -> bool:
        with self._lock:
            if not self.client or not self.mute_param_found or self.is_muted == muted:
                return False
            try:
                self.client.send_message(f"/avatar/parameters/{self.mute_param_name}", float(muted))
            except Exception as exc:
                self.logger.log("error", f"Could not send VRChat mute state: {exc}")
                return False
            self.is_muted = muted
            self._expected_value = muted
            self._expected_deadline = time.monotonic() + 1.5
        self.logger.log("info", f"VRChat set by bridge -> {muted}")
        return True

    def _handle_deafen(self, value: bool) -> None:
        now = time.monotonic()
        with self._lock:
            initial = not self.deafen_param_found
            previous = self.is_deafened
            self.deafen_param_found = True
            self.is_deafened = value
            expected = (
                self._expected_deafen is not None
                and now <= self._expected_deafen_deadline
                and value == self._expected_deafen
            )
            self._expected_deafen = None
            self._expected_deafen_deadline = 0.0
            changed = initial or value != previous
        if expected:
            self.logger.log("debug", f"Consumed expected VRChat deafen OSC echo -> {value}")
            return
        if changed:
            self.logger.log("info", f"VRChat deafen parameter changed -> {value}")
            if self.callback:
                self.callback("vrchat_deafen", value, initial)

    def set_deafen(self, deafened: bool) -> bool:
        with self._lock:
            if (not self.client or not self.deafen_param_found
                    or self.is_deafened == deafened or not self.deafen_param_name):
                return False
            try:
                self.client.send_message(
                    f"/avatar/parameters/{self.deafen_param_name}", float(deafened))
            except Exception as exc:
                self.logger.log("error", f"Could not send VRChat deafen state: {exc}")
                return False
            self.is_deafened = deafened
            self._expected_deafen = deafened
            self._expected_deafen_deadline = time.monotonic() + 1.5
        self.logger.log("info", f"VRChat deafen parameter set -> {deafened}")
        return True


class MicSyncSystem:
    """Serial coordinator implementing master and last-change-wins modes."""

    def __init__(self):
        self.config = ConfigManager(CONFIG_FILE)
        self.logger = MicSyncLogger(LOG_FILE)
        if self.config.get("logging_enabled"):
            self.logger.enable()
        self.discord = DiscordHandler(self.config.get("discord_mute_hotkey"), self.logger, self.config)
        self.vrchat = VRChatOSCHandler(
            self.config.get("vrchat_osc_ip"),
            self.config.get("vrchat_osc_send_port"),
            self.config.get("vrchat_osc_receive_port"),
            self.logger,
        )
        self.vrchat.set_parameter_names(
            self.config.get("mute_parameter_name"),
            self.config.get("toggle_parameter_name"),
            self.config.get("deafen_parameter_name"),
        )
        self.discord.set_callback(self._on_state_change)
        self.vrchat.set_callback(self._on_state_change)
        self.system_enabled = bool(self.config.get("system_enabled"))
        self.sync_mode = self.config.get("sync_mode")
        self.deafen_sync_enabled = bool(self.config.get("deafen_sync_enabled"))
        self.deafen_sync_mode = self.config.get("deafen_sync_mode")
        self.last_action = "Waiting for VRChat"
        self.running = False
        self.gui: Optional[MicSyncGUI] = None
        self._event_lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._last_retry = 0.0

    def set_gui(self, gui: "MicSyncGUI") -> None:
        self.gui = gui

    def _notify(self) -> None:
        if self.gui:
            self.gui.queue_update()

    def _on_state_change(self, source: str, value: bool, initial: bool = False) -> None:
        with self._event_lock:
            if source == "toggle":
                self.set_enabled(value)
                return
            if source in {"discord_deafen", "vrchat_deafen"}:
                self._handle_deafen_change(source, value, initial)
                return
            if not self.system_enabled:
                self.last_action = f"Observed {source}; sync is paused"
                self._notify()
                return
            if self.discord.is_deafened:
                self.last_action = "Mic sync paused while Discord is deafened"
                self._notify()
                return

            if self.sync_mode == MODE_DYNAMIC:
                if initial:
                    self.last_action = "Ready — waiting for either app to change"
                elif source == "discord":
                    self._set_vrchat(not value, "Discord changed")
                else:
                    self._set_discord(not value, "VRChat changed")
            elif self.sync_mode == MODE_VRC_MASTER:
                # A Discord change is corrected using the authoritative VRC state.
                if self.vrchat.mute_param_found:
                    self._set_discord(not self.vrchat.is_muted, "VRChat is master")
            elif self.sync_mode == MODE_DISCORD_MASTER:
                if self.vrchat.mute_param_found and self.discord.is_muted is not None:
                    self._set_vrchat(not self.discord.is_muted, "Discord is master")
        self._notify()

    def _handle_deafen_change(self, source: str, value: bool, initial: bool) -> None:
        if not self.system_enabled or not self.deafen_sync_enabled:
            if source == "discord_deafen":
                self.last_action = (
                    "Mic sync paused while Discord is deafened"
                    if value else "Discord deafen changed; deafen bridge is off"
                )
            self._notify()
            return
        mode = self.deafen_sync_mode
        if mode == MODE_DYNAMIC:
            if initial:
                self.last_action = "Deafen bridge ready — waiting for a change"
            elif source == "discord_deafen":
                self._set_vrchat_deafen(value, "Discord deafen changed")
            else:
                self._set_discord_deafen(value, "VRChat deafen changed")
        elif mode == MODE_VRC_MASTER:
            if self.vrchat.deafen_param_found and self.vrchat.is_deafened is not None:
                self._set_discord_deafen(self.vrchat.is_deafened, "VRChat deafen is master")
        elif mode == MODE_DISCORD_MASTER:
            if self.discord.is_deafened is not None and self.vrchat.deafen_param_found:
                self._set_vrchat_deafen(self.discord.is_deafened, "Discord deafen is master")
        self._notify()

    def _set_discord(self, target: bool, reason: str) -> None:
        if not self.discord.reader_ready or self.discord.is_muted is None:
            self.last_action = "Waiting for Discord accessibility state"
            return
        changed = self.discord.set_mute(target)
        self.last_action = f"{reason} → Discord {'muted' if target else 'live'}"
        self.logger.log("info", self.last_action + ("" if changed else " (already correct)"))

    def _set_vrchat(self, target: bool, reason: str) -> None:
        changed = self.vrchat.set_mute(target)
        self.last_action = f"{reason} → VRChat {'muted' if target else 'live'}"
        self.logger.log("info", self.last_action + ("" if changed else " (already correct)"))

    def _set_discord_deafen(self, target: bool, reason: str) -> None:
        if not self.discord.reader_ready or self.discord.is_deafened is None:
            self.last_action = "Waiting for Discord deafen accessibility state"
            return
        changed = self.discord.set_deafen(target)
        self.last_action = f"{reason} → Discord {'deafened' if target else 'undeafened'}"
        self.logger.log("info", self.last_action + ("" if changed else " (already correct)"))

    def _set_vrchat_deafen(self, target: bool, reason: str) -> None:
        changed = self.vrchat.set_deafen(target)
        self.last_action = f"{reason} → VRChat deafen parameter {target}"
        self.logger.log("info", self.last_action + ("" if changed else " (already correct)"))

    def synchronize_now(self) -> None:
        with self._event_lock:
            if not self.system_enabled:
                self.last_action = "Sync paused"
            elif self.discord.is_deafened:
                self.last_action = "Mic sync paused while Discord is deafened"
            elif not self.vrchat.mute_param_found:
                self.last_action = "Waiting for VRChat MuteSelf"
            elif self.sync_mode == MODE_VRC_MASTER:
                self._set_discord(not self.vrchat.is_muted, "VRChat is master")
            elif self.sync_mode == MODE_DISCORD_MASTER:
                if self.discord.is_muted is None:
                    self.last_action = "Waiting for Discord accessibility state"
                else:
                    self._set_vrchat(not self.discord.is_muted, "Discord is master")
            else:
                self.last_action = "Ready — waiting for either app to change"
        self._notify()

    def set_mode(self, mode: str) -> None:
        if mode not in VALID_MODES:
            return
        self.sync_mode = mode
        self.config.update("sync_mode", mode)
        self.synchronize_now()

    def synchronize_deafen_now(self) -> None:
        with self._event_lock:
            if not self.system_enabled or not self.deafen_sync_enabled:
                return
            if not self.vrchat.deafen_param_found:
                self.last_action = f"Waiting for VRChat {self.vrchat.deafen_param_name} parameter"
            elif self.deafen_sync_mode == MODE_VRC_MASTER:
                if self.vrchat.is_deafened is not None:
                    self._set_discord_deafen(self.vrchat.is_deafened, "VRChat deafen is master")
            elif self.deafen_sync_mode == MODE_DISCORD_MASTER:
                if self.discord.is_deafened is not None:
                    self._set_vrchat_deafen(self.discord.is_deafened, "Discord deafen is master")
            else:
                self.last_action = "Deafen bridge ready — waiting for a change"
        self._notify()

    def set_deafen_mode(self, mode: str) -> None:
        if mode not in VALID_MODES:
            return
        self.deafen_sync_mode = mode
        self.config.update("deafen_sync_mode", mode)
        self.synchronize_deafen_now()

    def set_deafen_enabled(self, enabled: bool) -> None:
        self.deafen_sync_enabled = bool(enabled)
        self.config.update("deafen_sync_enabled", self.deafen_sync_enabled)
        if enabled:
            self.synchronize_deafen_now()
        else:
            self.last_action = "Deafen parameter bridge disabled"
            self._notify()

    def set_enabled(self, enabled: bool) -> None:
        self.system_enabled = bool(enabled)
        self.config.update("system_enabled", self.system_enabled)
        if enabled:
            self.synchronize_now()
            self.synchronize_deafen_now()
        else:
            self.last_action = "Sync paused"
            self._notify()

    def start(self) -> None:
        self.vrchat.connect()
        self.discord.connect()
        self.running = True
        self._thread = threading.Thread(target=self._maintenance_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=1.5)
        self.discord.disconnect()
        self.vrchat.disconnect()
        self.logger.disable()

    def _maintenance_loop(self) -> None:
        while self.running:
            try:
                now = time.monotonic()
                if not self.vrchat.connected and now - self._last_retry > 5.0:
                    self.vrchat.connect()
                    self._last_retry = now
                if self.config.changed_on_disk():
                    self._apply_config()
                self._notify()
            except Exception as exc:
                self.logger.log("error", f"Maintenance error: {exc}")
            time.sleep(0.5)

    def _apply_config(self) -> None:
        logging_enabled = bool(self.config.get("logging_enabled"))
        if logging_enabled:
            self.logger.enable()
        else:
            self.logger.disable()
        self.system_enabled = bool(self.config.get("system_enabled"))
        mode = self.config.get("sync_mode")
        self.sync_mode = mode if mode in VALID_MODES else MODE_DYNAMIC
        deafen_mode = self.config.get("deafen_sync_mode")
        self.deafen_sync_mode = deafen_mode if deafen_mode in VALID_MODES else MODE_DYNAMIC
        self.deafen_sync_enabled = bool(self.config.get("deafen_sync_enabled"))
        self.discord.change_hotkeys(
            self.config.get("discord_mute_hotkey"),
            self.config.get("discord_deafen_hotkey"),
        )
        self.vrchat.set_parameter_names(
            self.config.get("mute_parameter_name"),
            self.config.get("toggle_parameter_name"),
            self.config.get("deafen_parameter_name"),
        )
        send_port = int(self.config.get("vrchat_osc_send_port"))
        receive_port = int(self.config.get("vrchat_osc_receive_port"))
        if (send_port, receive_port) != (self.vrchat.send_port, self.vrchat.receive_port):
            self.vrchat.restart(send_port, receive_port)


class MicSyncGUI:
    BG = "#0b1020"
    PANEL = "#151c2f"
    PANEL_2 = "#1c2540"
    TEXT = "#eef2ff"
    MUTED = "#9aa6c3"
    BLUE = "#5865f2"
    CYAN = "#2dd4bf"
    RED = "#fb7185"

    def __init__(self, system: MicSyncSystem):
        self.system = system
        system.set_gui(self)
        self.root = tk.Tk()
        self.root.title("Mic Bridge")
        self.root.geometry("820x880")
        self.root.minsize(760, 800)
        self.root.configure(bg=self.BG)
        self.update_queue: queue.Queue[bool] = queue.Queue(maxsize=1)
        self.mode_buttons: dict[str, tk.Button] = {}
        self.deafen_mode_buttons: dict[str, tk.Button] = {}
        self._autosave_job: Optional[str] = None
        self._record_target: Optional[tk.StringVar] = None
        self._record_button: Optional[tk.Button] = None
        self._record_hook = None
        self._recorded_keys: list[str] = []
        self._record_finish_job: Optional[str] = None
        self._record_timeout_job: Optional[str] = None
        self._record_queue: queue.Queue[str] = queue.Queue()
        self._build_styles()
        self._build_ui()
        self._poll()

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TEntry", fieldbackground=self.PANEL_2, foreground=self.TEXT,
                        insertcolor=self.TEXT, bordercolor="#33415f", padding=8)
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT,
                        font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", self.PANEL)])

    def _label(self, parent: tk.Widget, text: str, size: int = 10,
               color: Optional[str] = None, weight: str = "normal") -> tk.Label:
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color or self.TEXT,
                        font=("Segoe UI", size, weight))

    def _panel(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(parent, bg=self.PANEL, highlightthickness=1,
                        highlightbackground="#202b49", padx=18, pady=16)

    def _keybind_field(self, parent: tk.Widget, variable: tk.StringVar,
                       row: int, padx: tuple[int, int]) -> ttk.Entry:
        container = tk.Frame(parent, bg=self.PANEL)
        container.grid(row=row, column=0, columnspan=2, sticky="ew", padx=padx, pady=(4, 12))
        record = tk.Button(
            container, text="●", width=2, relief="flat", bd=0,
            bg=self.PANEL_2, fg=self.RED, activebackground="#31233a",
            activeforeground="#ff8da1", font=("Segoe UI Symbol", 14, "bold"),
            cursor="hand2")
        record.configure(command=lambda: self._start_key_recording(variable, record))
        record.pack(side="left", padx=(0, 7))
        entry = ttk.Entry(container, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=self.BG, padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=self.BG)
        header.pack(fill="x", pady=(0, 18))
        self._label(header, "MIC BRIDGE", 22, self.TEXT, "bold").pack(side="left")
        self._label(header, "Discord  ↔  VRChat", 11, self.MUTED).pack(side="left", padx=16, pady=(7, 0))
        self.enabled_var = tk.BooleanVar(value=self.system.system_enabled)
        toggle = tk.Checkbutton(header, text="SYNC ON", variable=self.enabled_var,
                                command=lambda: self.system.set_enabled(self.enabled_var.get()),
                                bg=self.BG, fg=self.CYAN, activebackground=self.BG,
                                activeforeground=self.CYAN, selectcolor=self.PANEL,
                                font=("Segoe UI", 10, "bold"), cursor="hand2")
        toggle.pack(side="right", pady=(5, 0))

        status = self._panel(outer)
        status.pack(fill="x", pady=(0, 14))
        for column in (0, 1):
            status.columnconfigure(column, weight=1, uniform="state")
        self.discord_title = self._label(status, "DISCORD", 9, self.MUTED, "bold")
        self.discord_title.grid(row=0, column=0, sticky="w")
        self.vrc_title = self._label(status, "VRCHAT", 9, self.MUTED, "bold")
        self.vrc_title.grid(row=0, column=1, sticky="w", padx=(24, 0))
        self.discord_state = self._label(status, "Unmuted", 19, self.CYAN, "bold")
        self.discord_state.grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.vrc_state = self._label(status, "Waiting…", 19, self.MUTED, "bold")
        self.vrc_state.grid(row=1, column=1, sticky="w", padx=(24, 0), pady=(3, 0))
        self.discord_ready = self._label(status, "Global keybind", 9, self.MUTED)
        self.discord_ready.grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.vrc_ready = self._label(status, "OSC / MuteSelf", 9, self.MUTED)
        self.vrc_ready.grid(row=2, column=1, sticky="w", padx=(24, 0), pady=(5, 0))
        self.discord_deafen = self._label(status, "Deafen: waiting", 9, self.MUTED, "bold")
        self.discord_deafen.grid(row=3, column=0, sticky="w", pady=(7, 0))
        self.vrc_deafen = self._label(status, "Deafen param: waiting", 9, self.MUTED, "bold")
        self.vrc_deafen.grid(row=3, column=1, sticky="w", padx=(24, 0), pady=(7, 0))

        mode_panel = self._panel(outer)
        mode_panel.pack(fill="x", pady=(0, 14))
        self._label(mode_panel, "SYNC DIRECTION", 10, self.MUTED, "bold").pack(anchor="w")
        choices = tk.Frame(mode_panel, bg=self.PANEL)
        choices.pack(fill="x", pady=(12, 0))
        items = [
            (MODE_VRC_MASTER, "VRChat master", "VRC always decides"),
            (MODE_DYNAMIC, "Dynamic", "Last user change wins"),
            (MODE_DISCORD_MASTER, "Discord master", "Discord always decides"),
        ]
        for index, (mode, title, subtitle) in enumerate(items):
            choices.columnconfigure(index, weight=1, uniform="mode")
            button = tk.Button(choices, text=f"{title}\n{subtitle}", justify="left",
                               command=lambda selected=mode: self.system.set_mode(selected),
                               relief="flat", bd=0, padx=14, pady=10, cursor="hand2",
                               font=("Segoe UI", 9, "bold"))
            button.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            self.mode_buttons[mode] = button
        self._refresh_mode_buttons()

        deafen_panel = self._panel(outer)
        deafen_panel.pack(fill="x", pady=(0, 14))
        deafen_header = tk.Frame(deafen_panel, bg=self.PANEL)
        deafen_header.pack(fill="x")
        self._label(deafen_header, "DEAFEN PARAMETER BRIDGE", 10, self.MUTED, "bold").pack(side="left")
        self.deafen_enabled_var = tk.BooleanVar(value=self.system.deafen_sync_enabled)
        tk.Checkbutton(
            deafen_header, text="ENABLED", variable=self.deafen_enabled_var,
            command=lambda: self.system.set_deafen_enabled(self.deafen_enabled_var.get()),
            bg=self.PANEL, fg=self.CYAN, activebackground=self.PANEL,
            activeforeground=self.CYAN, selectcolor=self.PANEL_2,
            font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="right")
        deafen_choices = tk.Frame(deafen_panel, bg=self.PANEL)
        deafen_choices.pack(fill="x", pady=(10, 0))
        for index, (mode, title, subtitle) in enumerate(items):
            deafen_choices.columnconfigure(index, weight=1, uniform="deafen_mode")
            button = tk.Button(
                deafen_choices, text=f"{title}\n{subtitle}", justify="left",
                command=lambda selected=mode: self.system.set_deafen_mode(selected),
                relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
                font=("Segoe UI", 9, "bold"))
            button.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            self.deafen_mode_buttons[mode] = button
        self._refresh_mode_buttons()

        settings = self._panel(outer)
        settings.pack(fill="both", expand=True)
        self._label(settings, "CONNECTION SETTINGS", 10, self.MUTED, "bold").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)
        self._label(settings, "Discord mute keybind", 9, self.MUTED).grid(row=1, column=0, sticky="w")
        self.hotkey_var = tk.StringVar(value=self.system.config.get("discord_mute_hotkey"))
        hotkey = self._keybind_field(settings, self.hotkey_var, 2, (0, 12))
        hotkey.bind("<Return>", lambda _event: self._save_settings())
        self._label(settings, "OSC send / receive ports", 9, self.MUTED).grid(row=1, column=2, sticky="w")
        ports = tk.Frame(settings, bg=self.PANEL)
        ports.grid(row=2, column=2, columnspan=2, sticky="ew", pady=(4, 12))
        for column in (0, 1):
            ports.columnconfigure(column, weight=1)
        self.send_var = tk.StringVar(value=str(self.system.config.get("vrchat_osc_send_port")))
        self.receive_var = tk.StringVar(value=str(self.system.config.get("vrchat_osc_receive_port")))
        ttk.Entry(ports, textvariable=self.send_var, width=8).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Entry(ports, textvariable=self.receive_var, width=8).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._label(settings, "Discord deafen keybind", 9, self.MUTED).grid(row=3, column=0, sticky="w")
        self.deafen_hotkey_var = tk.StringVar(value=self.system.config.get("discord_deafen_hotkey"))
        self._keybind_field(settings, self.deafen_hotkey_var, 4, (0, 12))
        self._label(settings, "VRChat deafen bool parameter", 9, self.MUTED).grid(
            row=3, column=2, sticky="w")
        self.deafen_param_var = tk.StringVar(value=self.system.config.get("deafen_parameter_name"))
        ttk.Entry(settings, textvariable=self.deafen_param_var).grid(
            row=4, column=2, columnspan=2, sticky="ew", pady=(4, 12))
        self.logging_var = tk.BooleanVar(value=self.system.config.get("logging_enabled"))
        ttk.Checkbutton(settings, text="Write diagnostic log", variable=self.logging_var).grid(
            row=5, column=0, columnspan=4, sticky="w")
        self.action_label = self._label(settings, self.system.last_action, 9, self.MUTED)
        self.action_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(14, 0))
        for variable in (
            self.hotkey_var, self.send_var, self.receive_var,
            self.deafen_hotkey_var, self.deafen_param_var, self.logging_var,
        ):
            variable.trace_add("write", self._schedule_auto_save)

    def _refresh_mode_buttons(self) -> None:
        for mode, button in self.mode_buttons.items():
            selected = mode == self.system.sync_mode
            button.configure(bg=self.BLUE if selected else self.PANEL_2,
                             fg="white" if selected else self.MUTED,
                             activebackground=self.BLUE if selected else "#263252",
                             activeforeground="white")
        for mode, button in self.deafen_mode_buttons.items():
            selected = mode == self.system.deafen_sync_mode
            button.configure(bg=self.CYAN if selected else self.PANEL_2,
                             fg=self.BG if selected else self.MUTED,
                             activebackground=self.CYAN if selected else "#263252",
                             activeforeground=self.BG if selected else "white")

    @staticmethod
    def _normalize_recorded_key(name: str) -> str:
        aliases = {
            "left ctrl": "ctrl", "right ctrl": "ctrl",
            "left shift": "shift", "right shift": "shift",
            "left alt": "alt", "right alt": "alt",
            "left windows": "windows", "right windows": "windows",
            "left win": "windows", "right win": "windows",
        }
        cleaned = (name or "").strip().lower()
        return aliases.get(cleaned, cleaned)

    def _start_key_recording(self, target: tk.StringVar, button: tk.Button) -> None:
        if self._record_target is target:
            self._stop_key_recording(False, "Key recording cancelled")
            return
        if self._record_target is not None:
            self._stop_key_recording(False, "")
        if keyboard is None:
            self.system.last_action = "The keyboard package is unavailable"
            self.queue_update()
            return
        self._record_target = target
        self._record_button = button
        self._recorded_keys = []
        self.system.discord.commands_suspended = True
        button.configure(text="◉", bg=self.RED, fg="white")
        try:
            self._record_hook = keyboard.hook(self._capture_recorded_key, suppress=False)
        except Exception as exc:
            self._stop_key_recording(False, f"Could not record keys: {exc}")
            return
        self._record_timeout_job = self.root.after(
            30_000, lambda: self._stop_key_recording(False, "Key recording timed out"))
        self.system.last_action = "Recording keybind — press a key (30 second timeout)"
        self.queue_update()

    def _capture_recorded_key(self, event: Any) -> None:
        if getattr(event, "event_type", None) != "down":
            return
        name = self._normalize_recorded_key(getattr(event, "name", ""))
        if name:
            self._record_queue.put(name)

    def _drain_recorded_keys(self) -> None:
        while True:
            try:
                name = self._record_queue.get_nowait()
            except queue.Empty:
                break
            if self._record_target is None or name in self._recorded_keys:
                continue
            self._recorded_keys.append(name)
            if self._record_finish_job is None:
                self._record_finish_job = self.root.after(
                    5_000, lambda: self._stop_key_recording(True, "Keybind recorded and saved"))
                self.system.last_action = "First key recorded — adding keys for 5 seconds…"
                self.queue_update()

    def _stop_key_recording(self, save: bool, message: str) -> None:
        target = self._record_target
        if self._record_hook is not None and keyboard is not None:
            try:
                keyboard.unhook(self._record_hook)
            except Exception:
                pass
        self._record_hook = None
        for job_name in ("_record_finish_job", "_record_timeout_job"):
            job = getattr(self, job_name)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, job_name, None)
        if self._record_button is not None:
            self._record_button.configure(text="●", bg=self.PANEL_2, fg=self.RED)
        self._record_target = None
        self._record_button = None
        self.system.discord.commands_suspended = False
        if save and target is not None and self._recorded_keys:
            target.set("+".join(self._recorded_keys))
            if self._autosave_job is not None:
                self.root.after_cancel(self._autosave_job)
                self._autosave_job = None
            self._save_settings(automatic=True)
        self._recorded_keys = []
        if message:
            self.system.last_action = message
            self.queue_update()

    def _schedule_auto_save(self, *_args: Any) -> None:
        if self._autosave_job is not None:
            self.root.after_cancel(self._autosave_job)
        self._autosave_job = self.root.after(500, self._auto_save)

    def _auto_save(self) -> None:
        self._autosave_job = None
        self._save_settings(automatic=True)

    def _save_settings(self, automatic: bool = False) -> bool:
        hotkey = self.hotkey_var.get().strip()
        try:
            send_port = int(self.send_var.get())
            receive_port = int(self.receive_var.get())
            if not (1 <= send_port <= 65535 and 1 <= receive_port <= 65535):
                raise ValueError
        except ValueError:
            self.system.last_action = (
                "Waiting for valid port numbers…" if automatic
                else "Ports must be numbers from 1 to 65535"
            )
            self.queue_update()
            return False
        self.system.config.update("discord_mute_hotkey", hotkey)
        deafen_hotkey = self.deafen_hotkey_var.get().strip()
        deafen_param = self.deafen_param_var.get().strip()
        self.system.config.update("discord_deafen_hotkey", deafen_hotkey)
        self.system.config.update("deafen_parameter_name", deafen_param)
        self.system.config.update("vrchat_osc_send_port", send_port)
        self.system.config.update("vrchat_osc_receive_port", receive_port)
        logging_on = self.logging_var.get()
        self.system.config.update("logging_enabled", logging_on)
        self.system._apply_config()
        self.system.last_action = "Settings saved automatically" if automatic else "Settings saved"
        self.queue_update()
        return True

    def queue_update(self) -> None:
        try:
            self.update_queue.put_nowait(True)
        except queue.Full:
            pass

    def _poll(self) -> None:
        self._drain_recorded_keys()
        try:
            self.update_queue.get_nowait()
        except queue.Empty:
            pass
        self.enabled_var.set(self.system.system_enabled)
        self.deafen_enabled_var.set(self.system.deafen_sync_enabled)
        self._refresh_mode_buttons()
        discord_muted = self.system.discord.is_muted
        if discord_muted is None:
            self.discord_state.configure(text="WAITING", fg=self.MUTED)
        else:
            self.discord_state.configure(text="MUTED" if discord_muted else "LIVE",
                                         fg=self.RED if discord_muted else self.CYAN)
        if self.system.discord.reader_ready:
            self.discord_ready.configure(text="Accessibility reader ready", fg=self.CYAN)
        elif self.system.discord.connected:
            self.discord_ready.configure(text="Searching Discord UI…", fg=self.MUTED)
        else:
            self.discord_ready.configure(text="Accessibility reader unavailable", fg=self.RED)
        deafened = self.system.discord.is_deafened
        if deafened is None:
            self.discord_deafen.configure(text="Deafen: waiting", fg=self.MUTED)
        else:
            self.discord_deafen.configure(
                text="DEAFENED — mic sync paused" if deafened else "Deafen: off",
                fg=self.RED if deafened else self.MUTED)
        if self.system.deafen_sync_enabled:
            if self.system.vrchat.deafen_param_found:
                state = self.system.vrchat.is_deafened
                self.vrc_deafen.configure(text=f"{self.system.vrchat.deafen_param_name}: {state}", fg=self.CYAN)
            else:
                self.vrc_deafen.configure(
                    text=f"Waiting for {self.system.vrchat.deafen_param_name}", fg=self.MUTED)
        else:
            self.vrc_deafen.configure(text="Deafen parameter bridge: off", fg=self.MUTED)
        if self.system.vrchat.mute_param_found:
            vrc_muted = self.system.vrchat.is_muted
            self.vrc_state.configure(text="MUTED" if vrc_muted else "LIVE",
                                     fg=self.RED if vrc_muted else self.CYAN)
            self.vrc_ready.configure(text="MuteSelf detected", fg=self.CYAN)
        else:
            self.vrc_state.configure(text="WAITING", fg=self.MUTED)
            connection = "OSC ready; waiting for MuteSelf" if self.system.vrchat.connected else "OSC unavailable"
            self.vrc_ready.configure(text=connection, fg=self.MUTED if self.system.vrchat.connected else self.RED)
        self.action_label.configure(text=self.system.last_action)
        self.root.after(120, self._poll)

    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.mainloop()

    def _close(self) -> None:
        if self._record_target is not None:
            self._stop_key_recording(False, "")
        if self._autosave_job is not None:
            self.root.after_cancel(self._autosave_job)
            self._autosave_job = None
        self.system.stop()
        self.root.destroy()


def main() -> None:
    missing = []
    if keyboard is None:
        missing.append("keyboard")
    if dispatcher is None:
        missing.append("python-osc")
    for package in ("pywinauto", "psutil"):
        if importlib.util.find_spec(package) is None:
            missing.append(package)
    if missing:
        print("Missing package(s): " + ", ".join(missing))
        print("Install with: pip install keyboard python-osc pywinauto psutil")
        return
    system = MicSyncSystem()
    system.start()
    MicSyncGUI(system).run()


if __name__ == "__main__":
    main()
