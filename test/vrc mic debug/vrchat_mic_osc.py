import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from pythonosc import dispatcher
from pythonosc import osc_server
from pythonosc import udp_client

APP_TITLE = "VRChat Mic OSC Toggle (MuteSelf + /input/Voice)"
CONFIG_FILE = "vrc_mic_osc_config.json"

DEFAULTS = {
    # VRChat defaults (commonly):
    # - VRChat LISTENS on 9000 (incoming from your app)
    # - VRChat SENDS on 9001 (outgoing to your app)
    "vrchat_ip": "127.0.0.1",
    "vrchat_in_port": 9000,   # where we SEND /input/Voice
    "listen_ip": "127.0.0.1",
    "listen_port": 9001,      # where we LISTEN for /avatar/parameters/MuteSelf
    "press_ms": 80,           # how long to wait between 1 and 0
    "extra_release": True,    # send an extra 0 after a short delay (workaround)
    "extra_release_ms": 120,
    "log_level": "INFO",
}

# ---------------- Logging ----------------
logger = logging.getLogger("vrc_mic")
logger.setLevel(logging.DEBUG)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                cfg.update(disk)
        except Exception as e:
            logger.warning("Failed to load config, using defaults: %s", e)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        logger.info("Saved config to %s", CONFIG_FILE)
    except Exception as e:
        logger.error("Failed to save config: %s", e)


# ---------------- OSC Server Thread ----------------
class OscListener:
    """
    Listens for VRChat outgoing OSC and puts updates into a thread-safe queue.
    """
    def __init__(self, listen_ip, listen_port, out_queue: queue.Queue):
        self.listen_ip = listen_ip
        self.listen_port = int(listen_port)
        self.q = out_queue

        self._server = None
        self._thread = None
        self._stop_evt = threading.Event()

    def start(self):
        self.stop()

        disp = dispatcher.Dispatcher()
        # MuteSelf is commonly present as an outgoing avatar parameter.
        disp.map("/avatar/parameters/MuteSelf", self._on_mute_self)
        # (Optional) log any incoming address for debugging if you want:
        # disp.set_default_handler(self._on_any)

        self._server = osc_server.ThreadingOSCUDPServer(
            (self.listen_ip, self.listen_port),
            disp
        )

        def run():
            logger.info("OSC listener starting on %s:%s", self.listen_ip, self.listen_port)
            try:
                self._server.serve_forever()
            except Exception as e:
                logger.error("OSC listener error: %s", e)
            finally:
                logger.info("OSC listener stopped")

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self._server = None
        self._thread = None

    def _on_mute_self(self, address, *args):
        # VRChat typically sends bool as True/False, sometimes as 0/1
        val = args[0] if args else None
        muted = None
        if isinstance(val, bool):
            muted = val
        elif isinstance(val, (int, float)):
            muted = (val != 0)
        else:
            muted = False

        logger.debug("RX %s %r -> muted=%s", address, val, muted)
        self.q.put(("mute_state", muted))

    def _on_any(self, address, *args):
        logger.debug("RX %s %r", address, args)


# ---------------- Main App ----------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("560x340")

        self.cfg = load_config()
        self._apply_log_level()

        self.q = queue.Queue()
        self.listener = None
        self.osc_client = None

        self.muted_state = None  # last known state from /avatar/parameters/MuteSelf

        self._build_ui()
        self._start_network()

        self.after(50, self._pump_queue)

    def _apply_log_level(self):
        lvl = str(self.cfg.get("log_level", "INFO")).upper()
        if lvl not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            lvl = "INFO"
        logger.info("Log level: %s", lvl)
        logger.setLevel(getattr(logging, lvl))

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=12, pady=10)

        # Status
        status_box = ttk.LabelFrame(frm, text="Status")
        status_box.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(status_box, textvariable=self.status_var, font=("Segoe UI", 11)).pack(anchor="w", padx=10, pady=8)

        self.mute_var = tk.StringVar(value="MuteSelf: (unknown)")
        ttk.Label(status_box, textvariable=self.mute_var).pack(anchor="w", padx=10, pady=(0, 8))

        # Controls
        ctrl_box = ttk.LabelFrame(frm, text="Controls")
        ctrl_box.pack(fill="x", **pad)

        btn_row = ttk.Frame(ctrl_box)
        btn_row.pack(fill="x", padx=10, pady=8)

        self.btn_toggle = ttk.Button(btn_row, text="Toggle Mic (/input/Voice)", command=self.on_toggle)
        self.btn_toggle.pack(side="left")

        ttk.Button(btn_row, text="Restart Listener", command=self._start_network).pack(side="left", padx=8)

        # Settings
        settings = ttk.LabelFrame(frm, text="Settings")
        settings.pack(fill="both", expand=True, **pad)

        grid = ttk.Frame(settings)
        grid.pack(fill="both", expand=True, padx=10, pady=10)

        def add_row(r, label, key):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=4)
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            ent = ttk.Entry(grid, textvariable=var, width=18)
            ent.grid(row=r, column=1, sticky="w", pady=4)
            return var

        self.var_vrc_ip = add_row(0, "VRChat IP:", "vrchat_ip")
        self.var_vrc_in_port = add_row(1, "VRChat IN port (send):", "vrchat_in_port")
        self.var_listen_ip = add_row(2, "Listen IP:", "listen_ip")
        self.var_listen_port = add_row(3, "Listen port (receive):", "listen_port")
        self.var_press_ms = add_row(4, "Press duration (ms):", "press_ms")
        self.var_extra_release_ms = add_row(5, "Extra release delay (ms):", "extra_release_ms")

        self.var_extra_release = tk.BooleanVar(value=bool(self.cfg.get("extra_release", True)))
        ttk.Checkbutton(grid, text="Send extra /input/Voice 0 (workaround)", variable=self.var_extra_release)\
            .grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 4))

        ttk.Label(grid, text="Log level:").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=(10, 4))
        self.var_log_level = tk.StringVar(value=str(self.cfg.get("log_level", "INFO")).upper())
        ttk.Combobox(grid, textvariable=self.var_log_level, values=["DEBUG", "INFO", "WARNING", "ERROR"], width=16, state="readonly")\
            .grid(row=7, column=1, sticky="w", pady=(10, 4))

        action_row = ttk.Frame(settings)
        action_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(action_row, text="Save Settings", command=self.on_save_settings).pack(side="left")

        self.tip_var = tk.StringVar(
            value="Tip: VRChat must have OSC enabled. For Toggle Voice mode, /input/Voice expects int 1 then 0."
        )
        ttk.Label(frm, textvariable=self.tip_var, foreground="gray").pack(anchor="w", padx=14, pady=(2, 0))

    def _start_network(self):
        # read current fields (if user changed but didn't save, we still use what's typed)
        try:
            listen_ip = self.var_listen_ip.get().strip()
            listen_port = int(self.var_listen_port.get().strip())
            vrc_ip = self.var_vrc_ip.get().strip()
            vrc_in_port = int(self.var_vrc_in_port.get().strip())
        except Exception as e:
            messagebox.showerror("Bad settings", f"Fix IP/ports first.\n\n{e}")
            return

        # OSC client (send to VRChat incoming)
        try:
            self.osc_client = udp_client.SimpleUDPClient(vrc_ip, vrc_in_port)
            logger.info("OSC client ready -> %s:%s", vrc_ip, vrc_in_port)
        except Exception as e:
            logger.error("Failed to create OSC client: %s", e)
            self.osc_client = None

        # OSC listener (receive from VRChat outgoing)
        try:
            if self.listener is not None:
                self.listener.stop()
            self.listener = OscListener(listen_ip, listen_port, self.q)
            self.listener.start()
            self.status_var.set(f"Listening on {listen_ip}:{listen_port} | Sending to {vrc_ip}:{vrc_in_port}")
        except Exception as e:
            logger.error("Failed to start listener: %s", e)
            self.status_var.set(f"Listener failed: {e}")

    def _pump_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                if not item:
                    continue
                kind = item[0]
                if kind == "mute_state":
                    muted = bool(item[1])
                    self.muted_state = muted
                    self.mute_var.set(f"MuteSelf: {'MUTED' if muted else 'UNMUTED'}")
        except queue.Empty:
            pass
        self.after(50, self._pump_queue)

    def on_toggle(self):
        if not self.osc_client:
            messagebox.showerror("Not ready", "OSC client not initialized. Check settings.")
            return

        # VRChat docs: Buttons expect int 1 then 0. /input/Voice behaves as toggle IF Toggle Voice enabled.
        # Workaround: send an extra 0 after a short delay to ensure release.
        try:
            press_ms = int(self.var_press_ms.get().strip())
        except Exception:
            press_ms = int(DEFAULTS["press_ms"])

        try:
            extra_release = bool(self.var_extra_release.get())
            extra_ms = int(self.var_extra_release_ms.get().strip())
        except Exception:
            extra_release = True
            extra_ms = int(DEFAULTS["extra_release_ms"])

        logger.info("TX /input/Voice 1 (int)")
        self.osc_client.send_message("/input/Voice", 1)  # int, not float

        def release_sequence():
            time.sleep(max(0.01, press_ms / 1000.0))
            logger.info("TX /input/Voice 0 (int)")
            self.osc_client.send_message("/input/Voice", 0)

            if extra_release:
                time.sleep(max(0.01, extra_ms / 1000.0))
                logger.info("TX /input/Voice 0 (extra release workaround)")
                self.osc_client.send_message("/input/Voice", 0)

        threading.Thread(target=release_sequence, daemon=True).start()

    def on_save_settings(self):
        try:
            self.cfg["vrchat_ip"] = self.var_vrc_ip.get().strip()
            self.cfg["vrchat_in_port"] = int(self.var_vrc_in_port.get().strip())
            self.cfg["listen_ip"] = self.var_listen_ip.get().strip()
            self.cfg["listen_port"] = int(self.var_listen_port.get().strip())
            self.cfg["press_ms"] = int(self.var_press_ms.get().strip())
            self.cfg["extra_release"] = bool(self.var_extra_release.get())
            self.cfg["extra_release_ms"] = int(self.var_extra_release_ms.get().strip())
            self.cfg["log_level"] = str(self.var_log_level.get()).upper()
        except Exception as e:
            messagebox.showerror("Bad settings", f"Fix settings first.\n\n{e}")
            return

        save_config(self.cfg)
        self._apply_log_level()
        self._start_network()

    def on_close(self):
        try:
            if self.listener:
                self.listener.stop()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
