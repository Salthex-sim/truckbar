"""TruckBar - a thin instrument bar for ATS / ETS2 that sits along the top of the screen.

Reads the game's live telemetry (see telemetry.py) and draws an instrument
cluster as one horizontal strip, so nothing covers the mirrors.

  Ctrl + Alt + B  open / close the settings window (works while driving)
  Drag            move the bar           (only when the mouse is free, e.g. in menus)
  Mouse wheel     bigger / smaller       ( " )
  Ctrl + wheel    more / less see-through

The bar itself never takes keyboard focus (so per-game keyboard/mouse profiles in
tools like Razer Synapse or Logitech G HUB stay on the game), stays on top of the
game, and remembers everything in %APPDATA%\\TruckBar\\settings.json. Closing the
settings window hands focus back to the game.

  pythonw truckbar.pyw                    run
  pythonw truckbar.pyw --exit-with-game   quit by itself once the game closes
  python  truckbar.pyw --demo             fake ATS readings, for checking the look
  python  truckbar.pyw --demo-ets2        fake ETS2 readings (metric, EU signs)

Set TRUCKBAR_SETTINGS to a file path to keep a separate settings file (for testing).
"""
import ctypes
import ctypes.wintypes as W
import json
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry as T  # noqa: E402
import controls as CTL  # noqa: E402

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # real pixels on scaled (125%+) displays
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

# ---------------------------------------------------------------- look ------
BG, PANEL, EDGE = "#15171b", "#1f2228", "#2c3038"
TXT, DIM, OFF = "#e8e8e8", "#8a8f98", "#3a3f48"
GREEN, AMBER, RED, BLUE = "#4cd964", "#ffbf3f", "#ff4d4d", "#4d9bff"
# wiper speeds: intermittent, low, high - three purples, light to deep
WIPER_PURPLES = ["#c9a9ff", "#9a6cf2", "#6b33d6"]
FONT = "Segoe UI"

BASE_H = 112                         # bar height at scale 1.0 (real pixels)
DROP = 44                            # how far the speed/RPM panel hangs below the bar
KEY = "#010203"                      # see-through colour: anything painted this is invisible
# tach bands as fractions of the governed RPM (for a typical 2100 RPM truck engine:
# 1090-1490 green, 1700 amber, 1900 red)
RPM_GREEN = (0.52, 0.71)
RPM_AMBER, RPM_RED = 0.81, 0.905
GAP = 6
FPS = 12                             # redraws per second - kept low, ATS is CPU-bound

# (key, label in settings, width at scale 1.0) - drawn left to right in this order
SECTIONS = [
    ("signals", "Turn signals + speed limit", 124),
    ("speed", "Speed, RPM, gear, cruise", 330),
    ("lamps", "Status lights + retarder steps", 280),
    ("warnings", "Warning lights", 156),
    ("compass", "Compass tape", 236),
    ("fuel", "Fuel, DEF, MPG, range / fuel stop", 200),
    ("gauges", "Gauges: oil, air, water, battery, brakes", 412),
    ("job", "Job: destination, distance, time", 280),
    ("weather", "Weather forecast (needs a weather director running)", 300),
    ("clock", "Clock + rest timer", 110),
]

SETTINGS = os.environ.get("TRUCKBAR_SETTINGS") or os.path.join(os.environ.get("APPDATA", "."), "TruckBar", "settings.json")
SETTINGS_DIR = os.path.dirname(os.path.abspath(SETTINGS))
# An optional forecast written by a separate weather program; the section only shows while it exists.
FORECAST = os.environ.get("TRUCKBAR_FORECAST") or os.path.join(os.environ.get("APPDATA", "."), "WeatherDirector", "forecast.json")
DEFAULTS = {"x": None, "y": 0, "scale": 1.0, "alpha": 0.85, "locked": False,
            "gear_names": "auto", "clock_24h": False, "fuel_margin_km": 120, "wiper_speeds": 3, "show": {k: True for k, _, _ in SECTIONS}}

MPS_TO_MPH, MPS_TO_KMH = 2.23694, 3.6
M_TO_MI, L_TO_GAL = 0.000621371, 0.264172
PSI_TO_BAR = 0.0689476
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Dashboard names for the gearboxes the game only numbers. The telemetry gives 1..N;
# an 18-speed counts LL, LH, 1L, 1H ... 8H, so gear 11 is 5L.
GEAR_NAMES = {
    18: ["LL", "LH"] + [f"{n}{s}" for n in range(1, 9) for s in "LH"],
    13: ["L", "1", "2", "3", "4"] + [f"{n}{s}" for n in range(5, 9) for s in "LH"],
}


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            saved = json.load(f)
        show = saved.pop("show", {})
        s.update(saved)
        s["show"].update(show)
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS)
    except Exception:
        pass


def game_running():
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=0x08000000).stdout.lower()
    return "amtrucks.exe" in out or "eurotrucks2.exe" in out


def focus_game():
    u = ctypes.windll.user32
    for title in ("American Truck Simulator", "Euro Truck Simulator 2"):
        h = u.FindWindowW(None, title)
        if h:
            u.SetForegroundWindow(h)
            return


def documents_folder():
    """The real Documents folder, wherever Windows (or OneDrive) has moved it."""
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:   # CSIDL_PERSONAL
        return buf.value
    return os.path.join(os.path.expanduser("~"), "Documents")


class ShifterLayout:
    """Gear names from the H-shifter layout the game is using - the same place the dash
    gets them. The game has one layout for every truck (g_hshifter_layout in the
    profile's config_local.cfg), so e.g. a 13-speed driven with an Eaton 18 layout
    shows 18-speed names. Built-in layouts have no file, so those fall back to numbers.
    Re-read every 10 s so a layout change in the game's options shows up."""

    def __init__(self):
        self.names, self.checked, self.game = {}, 0.0, None

    def get(self, game):
        now = time.time()
        if game != self.game or now - self.checked > 10:
            self.game, self.checked = game, now
            try:
                self.names = self._load(game)
            except Exception:
                self.names = {}
        return self.names

    @staticmethod
    def _load(game):
        root = os.path.join(documents_folder(), "Euro Truck Simulator 2" if game == 1 else "American Truck Simulator")
        cfgs = []
        for sub in ("steam_profiles", "profiles"):
            base = os.path.join(root, sub)
            if os.path.isdir(base):
                for p in os.listdir(base):
                    c = os.path.join(base, p, "config_local.cfg")
                    if os.path.isfile(c):
                        cfgs.append(c)
        if not cfgs:
            return {}
        cfg = max(cfgs, key=os.path.getmtime)            # the profile in use
        import re
        m = re.search(r'uset g_hshifter_layout "([^"]+)"', open(cfg, encoding="utf-8", errors="replace").read())
        if not m:
            return {}
        lay = os.path.join(os.path.dirname(cfg), m.group(1) + ".sii")
        if not os.path.isfile(lay):
            return {}
        names = {}
        txt = open(lay, encoding="utf-8", errors="replace").read()
        for blk in re.findall(r"direct_gearbox_gear\s*:\s*\S+\s*\{(.*?)\}", txt, re.S):
            g = re.search(r"\bgear:\s*(-?\d+)", blk)
            n = re.search(r'\bname:\s*"?([^"\r\n]*)"?', blk)
            if g and n and n.group(1).strip() and int(g.group(1)) not in names:
                names[int(g.group(1))] = n.group(1).strip()
        return names


LAYOUT = ShifterLayout()


def gear_name(t, style):
    g, n = t.gearDashboard, t.gears
    if g == 0:
        return "N"
    if style == "auto":
        if T.text(t.shifterType) == "hshifter":
            name = LAYOUT.get(t.game).get(g)
            if name:
                return name
        names = GEAR_NAMES.get(n) if g > 0 else None
        if names and g <= len(names):
            return names[g - 1]
    return f"R{-g}" if g < 0 else str(g)


# ------------------------------------------------------------- hotkey -------
class Hotkey(threading.Thread):
    """Ctrl+Alt+B anywhere, even while the game has focus. Sets .pressed."""
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312

    def __init__(self):
        super().__init__(daemon=True)
        self.pressed = False
        self.ok = False

    def run(self):
        u = ctypes.windll.user32
        self.ok = bool(u.RegisterHotKey(None, 1, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, ord("B")))
        msg = W.MSG()
        while self.ok and u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                self.pressed = True


# ------------------------------------------------------------- demo data ----
class Demo:
    """Stand-in telemetry so the look can be checked without the game."""

    def __init__(self, ets2=False):
        self.t0 = time.time()
        self.ets2 = ets2

    def read(self):
        t = T.Telemetry()
        k = time.time() - self.t0
        t.sdkActive, t.game = True, (1 if self.ets2 else 2)
        if self.ets2:
            t.speed = (78 + 8 * math.sin(k / 3)) / MPS_TO_KMH
            t.speedLimit = 80 / MPS_TO_KMH
            t.cruiseControl, t.cruiseControlSpeed = True, 85 / MPS_TO_KMH
        else:
            t.speed = (55 + 8 * math.sin(k / 3)) / MPS_TO_MPH
            t.speedLimit = 65 / MPS_TO_MPH
            t.cruiseControl, t.cruiseControlSpeed = True, 62 / MPS_TO_MPH
        t.engineRpm, t.engineRpmMax = 1500 + 520 * math.sin(k / 2), 2100   # sweeps into the red
        t.gearDashboard, t.gears = 11, 18
        t.retarderStepCount, t.retarderBrake = 4, int(k) % 5
        t.motorBrake = int(k / 2) % 2 == 0
        t.lightsParking = t.lightsBeamLow = True
        t.differentialLock = int(k / 4) % 2 == 1
        t.blinkerLeftActive = True
        t.blinkerLeftOn = int(k * 2) % 2 == 0
        t.fuel, t.fuelCapacity, t.fuelWarningFactor = 420, 1135, 0.15
        t.fuelAvgConsumption = 0.36            # L/km, about 6.5 mpg
        t.fuelRange = 420 / 0.36               # km - less than the route, so a fuel stop shows
        t.cargoMass = 18870                    # kg, 41,600 lb
        t.adblue, t.adblueCapacity = 60, 80
        t.airPressure, t.airPressureWarningLevel = 118, 65
        t.oilPressure, t.oilTemperature, t.waterTemperature = 55, 98, 88
        t.batteryVoltage, t.brakeTemperature = 13.8, 140 + 60 * math.sin(k / 5)
        t.electricEnabled = t.engineEnabled = True
        t.onJob = True
        t.heading = (1.0 - (47 + 25 * math.sin(k / 4)) / 360.0) % 1.0   # swings around NE
        t.cityDst, t.compDst, t.cargo = b"St. Louis", b"Central Civil Works", b"Construction Machinery Components"
        if self.ets2:
            t.engineRpmMax, t.gears, t.gearDashboard, t.batteryVoltage = 2500, 12, 9, 27.6
            t.cityDst, t.compDst, t.cargo = b"Hamburg", b"Kraftwerk", b"Transformer"
        t.routeDistance, t.routeTime = 1069000, 41 * 3600
        t.time_abs = 4 * 1440 + 14 * 60 + 31   # Fri 14:31
        t.restStop = 512
        return t


# ------------------------------------------------------------------ bar -----
class Bar:
    def __init__(self, demo=None, exit_with_game=False):
        self.s = load_settings()
        self.reader = Demo(ets2=(demo == "ets2")) if demo else T.Reader()
        self.exit_with_game = exit_with_game
        self.seen_game = False
        self.last_game_check = 0.0
        self.win = None                      # the settings window, when open
        self.hotkey = Hotkey()
        self.hotkey.start()
        # the wiper speed, followed from the player's own controls
        self.game_id = None
        self.wiper_level, self.wiper_changed = 0, 0.0
        self.watcher = None if demo else CTL.ControlWatcher(documents_folder(), lambda: self.game_id)
        if self.watcher:
            self.watcher.start()

        self.root = tk.Tk()
        self.root.title("TruckBar")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY)
        self.root.attributes("-transparentcolor", KEY)   # the gap beside the dropped panel
        self.cv = tk.Canvas(self.root, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.place()
        self.root.update_idletasks()
        self.no_focus()

        self.cv.bind("<ButtonPress-1>", self.drag_start)
        self.cv.bind("<B1-Motion>", self.drag_move)
        self.cv.bind("<ButtonRelease-1>", lambda e: save_settings(self.s))
        self.cv.bind("<MouseWheel>", self.wheel)
        self.cv.bind("<Button-3>", lambda e: self.toggle_settings())
        self.tick()

    # -- window plumbing ------------------------------------------------------
    def shown(self):
        return [(k, w) for k, _, w in SECTIONS if self.s["show"].get(k, True)
                and (k != "weather" or self.forecast() is not None)]

    def forecast(self):
        """The weather director's latest forecast, re-read every few seconds; None if there isn't a fresh one."""
        now = time.time()
        if now - getattr(self, "_fc_read", 0) > 5:
            self._fc_read = now
            try:
                with open(FORECAST, encoding="utf-8") as f:
                    fc = json.load(f)
                self._fc = fc if now - fc.get("updated", 0) < 6 * 3600 else None
            except (OSError, ValueError):
                self._fc = None
        return self._fc

    def size(self):
        k = self.s["scale"]
        base_w = GAP + sum(w + GAP for _, w in self.shown()) if self.shown() else 300
        drop = DROP if self.s["show"].get("speed", True) else 0
        return int(base_w * k), int((BASE_H + drop) * k)

    def place(self):
        w, h = self.size()
        if self.s["x"] is None:   # first run: centred along the top of the main screen
            self.s["x"] = (self.root.winfo_screenwidth() - w) // 2
        self.root.geometry(f"{w}x{h}+{int(self.s['x'])}+{int(self.s['y'])}")
        self.root.attributes("-alpha", self.s["alpha"])

    def no_focus(self):
        """WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW: never steals focus, no taskbar button."""
        u = ctypes.windll.user32
        hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        GWL_EXSTYLE, NOACTIVATE, TOOLWINDOW, APPWINDOW = -20, 0x08000000, 0x00000080, 0x00040000
        style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, (style | NOACTIVATE | TOOLWINDOW) & ~APPWINDOW)

    def drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self.s["locked"]:
            return
        dx, dy = self._drag
        self.s["x"], self.s["y"] = e.x_root - dx, e.y_root - dy
        self.root.geometry(f"+{self.s['x']}+{self.s['y']}")

    def wheel(self, e):
        step = 1 if e.delta > 0 else -1
        if e.state & 0x4:   # Ctrl: opacity
            self.s["alpha"] = round(min(1.0, max(0.2, self.s["alpha"] + 0.05 * step)), 2)
        else:
            self.s["scale"] = round(min(2.0, max(0.5, self.s["scale"] + 0.05 * step)), 2)
        self.changed()

    def changed(self, recentre=False):
        if recentre:
            self.s["x"] = None
        self.place()
        save_settings(self.s)

    # -- settings window --------------------------------------------------------
    def toggle_settings(self):
        if self.win is not None:
            self.close_settings()
            return
        s = self.s
        win = self.win = tk.Toplevel(self.root, bg=BG)
        win.title("TruckBar settings")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close_settings)
        pad = {"padx": 14, "pady": 3, "sticky": "w"}
        lab = {"bg": BG, "fg": TXT, "font": (FONT, 11)}
        chk = dict(lab, selectcolor=PANEL, activebackground=BG, activeforeground=TXT)
        row = 0

        tk.Label(win, text="Show on the bar", font=(FONT, 12, "bold"), bg=BG, fg=AMBER).grid(row=row, column=0, columnspan=2, padx=14, pady=(12, 4), sticky="w")
        row += 1
        self._vars = {}
        for key, label, _ in SECTIONS:
            v = tk.BooleanVar(value=s["show"].get(key, True))
            self._vars[key] = v
            tk.Checkbutton(win, text=label, variable=v, command=self.apply_sections, **chk).grid(row=row, column=0, columnspan=2, **pad)
            row += 1

        tk.Label(win, text="Look", font=(FONT, 12, "bold"), bg=BG, fg=AMBER).grid(row=row, column=0, columnspan=2, padx=14, pady=(12, 4), sticky="w")
        row += 1
        tk.Label(win, text="Opacity", **lab).grid(row=row, column=0, **pad)
        op = tk.Scale(win, from_=20, to=100, orient="horizontal", length=260, bg=BG, fg=TXT, highlightthickness=0,
                      troughcolor=PANEL, command=lambda v: self.set_live("alpha", int(v) / 100))
        op.set(int(s["alpha"] * 100))
        op.grid(row=row, column=1, padx=14, sticky="w")
        row += 1
        tk.Label(win, text="Size", **lab).grid(row=row, column=0, **pad)
        sz = tk.Scale(win, from_=50, to=200, orient="horizontal", length=260, bg=BG, fg=TXT, highlightthickness=0,
                      troughcolor=PANEL, command=lambda v: self.set_live("scale", int(v) / 100))
        sz.set(int(s["scale"] * 100))
        sz.grid(row=row, column=1, padx=14, sticky="w")
        row += 1
        self._gear = tk.BooleanVar(value=s.get("gear_names", "auto") == "auto")
        tk.Checkbutton(win, text="Gear names like the dash (5L, 5H) instead of 1-18", variable=self._gear,
                       command=lambda: self.set_live("gear_names", "auto" if self._gear.get() else "numbers"),
                       **chk).grid(row=row, column=0, columnspan=2, **pad)
        row += 1
        tk.Label(win, text="Fuel stop margin", **lab).grid(row=row, column=0, **pad)
        margins = [("25 mi / 40 km", 40), ("50 mi / 80 km", 80), ("75 mi / 120 km", 120), ("100 mi / 160 km", 160)]
        cur = min(margins, key=lambda m: abs(m[1] - s.get("fuel_margin_km", 120)))[0]
        self._margin = tk.StringVar(value=cur)
        om = tk.OptionMenu(win, self._margin, *[m[0] for m in margins],
                           command=lambda v: self.set_live("fuel_margin_km", dict(margins)[v]))
        om.configure(bg=PANEL, fg=TXT, activebackground=EDGE, activeforeground=TXT, highlightthickness=0,
                     relief="flat", font=(FONT, 10))
        om["menu"].configure(bg=PANEL, fg=TXT, font=(FONT, 10))
        om.grid(row=row, column=1, padx=14, sticky="w")
        row += 1
        tk.Label(win, text="Wiper speeds", **lab).grid(row=row, column=0, **pad)
        wopts = [("3 - intermittent, low, high", 3), ("2 - low, high", 2)]
        self._wipers = tk.StringVar(value=[n for n, v in wopts if v == s.get("wiper_speeds", 3)][0])
        wm = tk.OptionMenu(win, self._wipers, *[n for n, _ in wopts],
                           command=lambda v: self.set_live("wiper_speeds", dict(wopts)[v]))
        wm.configure(bg=PANEL, fg=TXT, activebackground=EDGE, activeforeground=TXT, highlightthickness=0,
                     relief="flat", font=(FONT, 10))
        wm["menu"].configure(bg=PANEL, fg=TXT, font=(FONT, 10))
        wm.grid(row=row, column=1, padx=14, sticky="w")
        row += 1
        self._clock = tk.BooleanVar(value=s.get("clock_24h", False))
        tk.Checkbutton(win, text="24-hour clock (off = 12-hour with AM/PM)", variable=self._clock,
                       command=lambda: self.set_live("clock_24h", self._clock.get()),
                       **chk).grid(row=row, column=0, columnspan=2, **pad)
        row += 1

        tk.Label(win, text="Position", font=(FONT, 12, "bold"), bg=BG, fg=AMBER).grid(row=row, column=0, columnspan=2, padx=14, pady=(12, 4), sticky="w")
        row += 1
        nud = tk.Frame(win, bg=BG)
        nud.grid(row=row, column=0, columnspan=2, padx=14, sticky="w")
        btn = {"bg": PANEL, "fg": TXT, "activebackground": EDGE, "activeforeground": TXT, "relief": "flat",
               "font": (FONT, 11), "width": 4}
        for i, (txt, dx, dy) in enumerate((("◀", -40, 0), ("▲", 0, -20), ("▼", 0, 20), ("▶", 40, 0))):
            tk.Button(nud, text=txt, command=lambda dx=dx, dy=dy: self.nudge(dx, dy), **btn).grid(row=0, column=i, padx=2)
        tk.Button(nud, text="Centre along top", command=lambda: self.changed(recentre=True) or self.set_live("y", 0),
                  **dict(btn, width=16)).grid(row=0, column=4, padx=(10, 0))
        row += 1
        self._lock = tk.BooleanVar(value=s["locked"])
        tk.Checkbutton(win, text="Lock position (no dragging)", variable=self._lock,
                       command=lambda: self.set_live("locked", self._lock.get()), **chk).grid(row=row, column=0, columnspan=2, **pad)
        row += 1

        foot = tk.Frame(win, bg=BG)
        foot.grid(row=row, column=0, columnspan=2, padx=14, pady=14, sticky="we")
        tk.Button(foot, text="Done - back to the game", command=self.close_settings, **dict(btn, width=24)).pack(side="left")
        tk.Button(foot, text="Quit TruckBar", command=self.root.destroy, **dict(btn, width=12)).pack(side="right")
        tk.Label(win, text="Ctrl + Alt + B opens and closes this window", bg=BG, fg=DIM, font=(FONT, 9)).grid(row=row + 1, column=0, columnspan=2, padx=14, pady=(0, 10), sticky="w")

        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
        win.geometry(f"+{max(0, x)}+{self.root.winfo_y() + self.root.winfo_height() + 20}")
        win.lift()
        win.focus_force()

    def close_settings(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None
        focus_game()

    def apply_sections(self):
        self.s["show"] = {k: v.get() for k, v in self._vars.items()}
        self.changed(recentre=True)

    def set_live(self, key, val):
        self.s[key] = val
        self.changed()

    def nudge(self, dx, dy):
        self.s["x"] = int(self.s["x"] or 0) + dx
        self.s["y"] = int(self.s["y"]) + dy
        self.changed()

    # -- main loop --------------------------------------------------------------
    def tick(self):
        try:
            if self.hotkey.pressed:
                self.hotkey.pressed = False
                self.toggle_settings()
            t = self.reader.read()
            if t is not None and t.plugin_revision and t.plugin_revision != T.PLUGIN_REVISION:
                self.draw_waiting(f"TruckBar needs scs-telemetry plugin revision {T.PLUGIN_REVISION}, "
                                  f"the game has revision {t.plugin_revision} - see the README")
            elif t is not None and t.sdkActive:
                self.seen_game = True
                self.game_id = t.game
                self.follow_controls(t)
                self.draw(t)
            else:
                self.draw_waiting()
            now = time.time()
            if self.exit_with_game and now - self.last_game_check > 5:
                self.last_game_check = now
                if game_running():
                    self.seen_game = True
                elif self.seen_game:
                    self.root.destroy()
                    return
        except Exception as ex:   # never die mid-drive; show it instead
            self.cv.delete("all")
            self.cv.create_text(10, 10, anchor="nw", fill=RED, font=(FONT, 10), text=f"TruckBar error: {ex}")
        self.root.after(int(1000 / FPS), self.tick)

    # -- drawing helpers --------------------------------------------------------
    def k(self, v):
        return v * self.s["scale"]

    def f(self, size, weight="normal"):
        return (FONT, max(6, int(round(size * self.s["scale"]))), weight)

    @staticmethod
    def fit(text, font, max_px):
        """Shorten text with an ellipsis so it fits in max_px at this font."""
        fm = tkfont.Font(font=font)
        if fm.measure(text) <= max_px:
            return text
        while text and fm.measure(text + "…") > max_px:
            text = text[:-1]
        return text.rstrip() + "…"

    def panel(self, w, h=BASE_H):
        k = self.k
        self.cv.create_rectangle(0, k(6), k(w), k(h - 6), fill=PANEL, outline=EDGE, width=1)

    def dial(self, cx, cy, r, value, vmax, label, text, color, red_from=None, ticks=6, vmin=0,
             bands=(), text_size=None, text_color=None, markers=()):
        """bands: (from, to, colour) painted on the rim; markers: (value, colour) notches on the rim."""
        k, cv = self.k, self.cv
        start, sweep = 210, -240
        box = (k(cx - r), k(cy - r), k(cx + r), k(cy + r))
        rim = max(1, k(3 if r < 40 else 4))
        cv.create_arc(*box, start=start, extent=sweep, style="arc", outline=OFF, width=rim)
        span = vmax - vmin
        ang = lambda v: start + sweep * max(0.0, min(1.0, (v - vmin) / span)) if span > 0 else start
        if red_from is not None and span > 0:
            bands = tuple(bands) + ((red_from, vmax, RED),)
        for lo, hi, col in bands:
            a0, a1 = ang(lo), ang(hi)
            cv.create_arc(*box, start=a0, extent=a1 - a0, style="arc", outline=col, width=rim)
        for i in range(ticks + 1):
            a = math.radians(start + sweep * i / ticks)
            cv.create_line(k(cx + (r - 5) * math.cos(a)), k(cy - (r - 5) * math.sin(a)),
                           k(cx + r * math.cos(a)), k(cy - r * math.sin(a)), fill=DIM, width=1)
        for mval, mcol in markers:
            if not mval:
                continue
            a = math.radians(ang(mval))
            cv.create_line(k(cx + (r - 9) * math.cos(a)), k(cy - (r - 9) * math.sin(a)),
                           k(cx + (r + 4) * math.cos(a)), k(cy - (r + 4) * math.sin(a)),
                           fill=mcol, width=max(2, k(4)), capstyle="round")
        a = math.radians(ang(value))
        cv.create_line(k(cx), k(cy), k(cx + (r - 4) * math.cos(a)), k(cy - (r - 4) * math.sin(a)),
                       fill=color, width=max(1, k(2.5 if r < 40 else 3.5)), capstyle="round")
        cv.create_oval(k(cx - 3), k(cy - 3), k(cx + 3), k(cy + 3), fill=color, outline="")
        size = text_size or (13 if r > 31 else 11)
        cv.create_text(k(cx), k(cy + r * 0.55), text=text, fill=text_color or color, font=self.f(size, "bold"))
        if label:   # big dials have no room below the rim - tuck the label under the number
            ly = cy + r * 0.55 + 26 if r >= 50 else cy + r + 10
            cv.create_text(k(cx), k(ly), text=label, fill=DIM, font=self.f(8 if r > 31 else 7))

    def badge(self, x, y, label, state_color, w=34, outline_only=False, text_color=None):
        k = self.k
        lit = state_color is not None
        fill = state_color if lit and not outline_only else ""
        self.cv.create_rectangle(k(x), k(y), k(x + w), k(y + 18), outline=state_color if lit else OFF,
                                 fill=fill, width=2 if outline_only else 1)
        tc = text_color or (state_color if outline_only else BG) if lit else DIM
        self.cv.create_text(k(x + w / 2), k(y + 9), text=label, fill=tc, font=self.f(8, "bold"))

    def arrow(self, x, y, left, on):
        """x is the tip; the shaft runs back toward the sign."""
        k = self.k
        d = 1 if left else -1
        pts = [x, y, x + 12 * d, y - 9, x + 12 * d, y - 4, x + 22 * d, y - 4,
               x + 22 * d, y + 4, x + 12 * d, y + 4, x + 12 * d, y + 9]
        self.cv.create_polygon(*[k(p) for p in pts], fill=GREEN if on else OFF, outline="")

    # -- wiper speed --------------------------------------------------------------
    def follow_controls(self, t):
        """The telemetry only has 'wipers on/off'. Count the player's own presses for the speed,
        and let the telemetry correct it."""
        steps = int(self.s.get("wiper_speeds", 3))
        if self.watcher:
            presses = self.watcher.take()
            if t.paused:                    # the game ignores the stalk while paused, so do we
                return
        else:   # demo: cycle through everything
            k = int(time.time() - getattr(self.reader, "t0", 0))
            presses = {"wipers": 1 if k % 3 == 0 and k != getattr(self, "_dk", -1) else 0, "wipersback": 0}
            if presses["wipers"] and self.wiper_level >= steps:              # back to off, then round again
                presses["wipers"], presses["wipersback"] = 0, steps
            self._dk = k
            t.lightsBeamHigh = (k % 8) < 4
            t.wipers = self.wiper_level > 0
        now = time.time()
        for _ in range(presses["wipers"]):
            self.wiper_level = min(self.wiper_level + 1, steps)    # the stalk stops at the top speed
            self.wiper_changed = now
        for _ in range(presses["wipersback"]):
            self.wiper_level = max(0, self.wiper_level - 1)
            self.wiper_changed = now
        # let the game correct us (allowing a moment for it to catch up with a press)
        if now - self.wiper_changed > 1.5:
            if not t.wipers:
                self.wiper_level = 0
            elif self.wiper_level == 0:
                self.wiper_level = 1

    # -- the bar ----------------------------------------------------------------
    def draw_waiting(self, message=None):
        cv = self.cv
        cv.delete("all")
        w, h = self.size()
        bh = self.k(BASE_H)
        cv.create_rectangle(0, 0, w, bh, fill=BG, outline=EDGE)
        cv.create_text(w / 2, bh / 2, fill=AMBER if message else DIM, font=self.f(12),
                       text=message or "TruckBar - waiting for ATS / ETS2 to start…   (Ctrl+Alt+B for settings)")

    def draw(self, t):
        cv = self.cv
        cv.delete("all")
        w, h = self.size()
        cv.create_rectangle(0, 0, w, self.k(BASE_H), fill=BG, outline=EDGE)   # the bar; below it is see-through
        ctx = {"ats": t.game != 1}
        ctx["sp_mul"], ctx["sp_unit"] = (MPS_TO_MPH, "mph") if ctx["ats"] else (MPS_TO_KMH, "km/h")
        ctx["limit"] = round(t.speedLimit * ctx["sp_mul"])
        x = GAP
        for key, width in self.shown():
            before = len(cv.find_all())
            if key == "speed":   # hangs below the bar: its own background, then the panel
                cv.create_rectangle(0, 0, self.k(width + 2 * GAP), self.k(BASE_H + DROP), fill=BG, outline=EDGE)
                cv.move(cv.find_all()[-1], -self.k(GAP), 0)
                self.panel(width, BASE_H + DROP - 2)
            else:
                self.panel(width)
            getattr(self, "s_" + key)(t, ctx)
            for item in cv.find_all()[before:]:   # sections draw at their own origin
                cv.move(item, self.k(x), 0)
            x += width + GAP

    # each section draws from x = 0 within its own width
    def s_signals(self, t, c):
        cv, k = self.cv, self.k
        self.arrow(8, 56, True, t.blinkerLeftOn)
        self.arrow(116, 56, False, t.blinkerRightOn)
        limit = c["limit"]
        # a smaller sign now - the limit is also notched on the speedometer
        if c["ats"]:
            cv.create_rectangle(k(40), k(24), k(84), k(88), fill="#f2f2f2" if limit else OFF, outline="#111", width=2)
            cv.create_text(k(62), k(39), text="SPEED\nLIMIT", fill="#111", font=self.f(6, "bold"), justify="center")
            cv.create_text(k(62), k(70), text=str(limit) if limit else "–", fill="#111", font=self.f(15, "bold"))
        else:
            cv.create_oval(k(40), k(36), k(84), k(80), fill="#f2f2f2" if limit else OFF, outline=RED, width=k(4))
            cv.create_text(k(62), k(58), text=str(limit) if limit else "–", fill="#111", font=self.f(14, "bold"))

    def s_speed(self, t, c):
        cv, k = self.cv, self.k
        speed = abs(t.speed) * c["sp_mul"]
        # compare what's shown, not the raw value: 55.4 shows as 55 and is not over a 55 limit
        over = round(speed) - c["limit"] if c["limit"] else -1
        sc = GREEN if over <= 0 else (AMBER if over <= 5 else RED)
        # speedometer: the biggest thing on the bar, with the posted limit (amber) and the
        # cruise set speed (blue while engaged, grey while set but off) notched on the rim
        cruise = t.cruiseControlSpeed * c["sp_mul"]
        self.dial(76, 76, 58, speed, 90 if c["ats"] else 140, c["sp_unit"].upper(), f"{speed:.0f}", sc,
                  ticks=9, text_size=26,
                  markers=((cruise if cruise >= 1 else None, BLUE if t.cruiseControl else DIM),
                           (c["limit"] or None, AMBER)))
        # tach: a little smaller, with economy / caution / red bands; the number goes amber,
        # then blinks red (red only, so it doesn't nag through every low-gear pull)
        rmax = t.engineRpmMax or 2500
        rpm = t.engineRpm
        if rpm >= rmax * RPM_RED:
            rc = RED if int(time.time() * 4) % 2 == 0 else BG
        elif rpm >= rmax * RPM_AMBER:
            rc = AMBER
        else:
            rc = TXT
        self.dial(206, 70, 46, rpm, rmax, "RPM", f"{rpm:.0f}", TXT, ticks=7, text_size=17, text_color=rc,
                  bands=((rmax * RPM_GREEN[0], rmax * RPM_GREEN[1], GREEN),
                         (rmax * RPM_AMBER, rmax * RPM_RED, AMBER),
                         (rmax * RPM_RED, rmax, RED)))
        gear = gear_name(t, self.s.get("gear_names", "auto"))
        cv.create_text(k(292), k(36), text=gear, fill=TXT, font=self.f(20 if len(gear) < 3 else 17, "bold"))
        cv.create_text(k(292), k(60), text="GEAR", fill=DIM, font=self.f(7))
        cc = round(t.cruiseControlSpeed * c["sp_mul"])
        cv.create_text(k(292), k(100), text=f"{cc}" if cc else "–", fill=GREEN if t.cruiseControl else DIM,
                       font=self.f(13, "bold"))
        cv.create_text(k(292), k(120), text="CRUISE", fill=DIM, font=self.f(7))

    def s_lamps(self, t, c):
        cv, k = self.cv, self.k
        x0, y1, y2, y3 = 10, 16, 44, 72
        steps, ret = t.retarderStepCount, t.retarderBrake
        self.badge(x0, y1, "P", "#f2f2f2" if t.parkBrake else None, 30)
        if steps:
            self.badge(x0 + 36, y1, f"R{ret}" if ret else "R", GREEN if ret else None, 34)
        self.badge(x0 + 76, y1, "J" if c["ats"] else "E", GREEN if t.motorBrake else None, 30)
        self.badge(x0 + 112, y1, "DIFF", AMBER if t.differentialLock else None, 40)
        self.badge(x0 + 158, y1, "LIFT", AMBER if t.liftAxleIndicator else None, 40)
        lvl = min(self.wiper_level, 3) if t.wipers else 0
        steps_w = int(self.s.get("wiper_speeds", 3))
        names = {1: "INT", 2: "LO", 3: "HI"} if steps_w >= 3 else {1: "LO", 2: "HI"}
        if lvl:
            col = WIPER_PURPLES[min(lvl, 3) - 1] if steps_w >= 3 else WIPER_PURPLES[lvl]
            self.badge(x0 + 204, y1, f"WIPE {names.get(lvl, lvl)}", col, 62,
                       text_color=BG if lvl == 1 else "#ffffff")
        else:
            self.badge(x0 + 204, y1, "WIPE", None, 62)
        self.badge(x0, y2, "SIDE", GREEN if t.lightsParking else None, 40)
        self.badge(x0 + 46, y2, "LOW", GREEN if t.lightsBeamLow else None, 40)
        # The telemetry's high beam is the switch: automatic high beams dip the lamps without it
        # changing. So with that option on, label it AUTO (blue = switch on, dim = off).
        if self.watcher and self.watcher.auto_high_beams:
            self.badge(x0 + 92, y2, "AUTO", BLUE if t.lightsBeamHigh else None, 40)
        else:
            self.badge(x0 + 92, y2, "HIGH", BLUE if t.lightsBeamHigh else None, 40)
        self.badge(x0 + 138, y2, "BCN", AMBER if t.lightsBeacon else None, 40)
        self.badge(x0 + 184, y2, "HAZ", AMBER if t.lightsHazard else None, 40)
        if steps:
            for i in range(steps):
                on = i < ret
                cv.create_rectangle(k(x0 + i * 16), k(y3 + 4), k(x0 + i * 16 + 12), k(y3 + 14),
                                    fill=GREEN if on else "", outline=GREEN if on else OFF)
            cv.create_text(k(x0 + steps * 16 + 6), k(y3 + 9), anchor="w", text=f"RETARDER {ret}/{steps}",
                           fill=GREEN if ret else DIM, font=self.f(8, "bold"))

    def s_warnings(self, t, c):
        live = t.electricEnabled
        warn = [("AIR", RED, t.airPressureWarning or t.airPressureEmergency),
                ("BRAKE", RED, t.airPressureEmergency),
                ("OIL", RED, t.oilPressureWarning),
                ("TEMP", RED, t.waterTemperatureWarning),
                ("FUEL", AMBER, t.fuelWarning),
                ("DEF" if c["ats"] else "ADBL", AMBER, t.adblueWarning),
                ("BATT", RED, t.batteryVoltageWarning)]
        for i, (lab, col, on) in enumerate(warn):
            self.badge(10 + (i % 3) * 46, 16 + (i // 3) * 28, lab, col if (on and live) else None, 42)

    def s_compass(self, t, c):
        """A scrolling tape: the bearing you're facing sits under the centre mark."""
        cv, k = self.cv, self.k
        bearing = (1.0 - t.heading) * 360.0 % 360.0
        x0, x1, mid = 10, 226, 118           # tape area within the section
        span = 120                           # degrees visible across the tape
        px_per_deg = (x1 - x0) / span
        y_top, y_base = 16, 58
        cv.create_rectangle(k(x0), k(y_top), k(x1), k(y_base), fill=BG, outline=OFF)
        cardinal = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}
        first = int(math.floor((bearing - span / 2) / 5.0)) * 5
        for d in range(first, first + span + 10, 5):
            x = mid + (d - bearing) * px_per_deg
            if x < x0 + 2 or x > x1 - 2:
                continue
            deg = d % 360
            if deg in cardinal:
                main = deg % 90 == 0
                cv.create_line(k(x), k(y_base - 14), k(x), k(y_base), fill=TXT if main else DIM, width=max(1, k(2)))
                cv.create_text(k(x), k(y_top + 13), text=cardinal[deg], fill=(AMBER if deg == 0 else TXT) if main else DIM,
                               font=self.f(13 if main else 10, "bold"))
            elif deg % 15 == 0:
                cv.create_line(k(x), k(y_base - 9), k(x), k(y_base), fill=DIM, width=1)
            else:
                cv.create_line(k(x), k(y_base - 5), k(x), k(y_base), fill=OFF, width=1)
        # centre mark - the direction you're facing
        cv.create_polygon(k(mid - 7), k(y_base + 9), k(mid + 7), k(y_base + 9), k(mid), k(y_base + 1),
                          fill=AMBER, outline="")
        cv.create_line(k(mid), k(y_top), k(mid), k(y_base), fill=AMBER, width=max(1, k(2)))
        names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        point = names[int((bearing + 11.25) // 22.5) % 16]
        cv.create_text(k(mid - 6), k(86), anchor="e", text=point, fill=TXT, font=self.f(16, "bold"))
        cv.create_text(k(mid + 6), k(86), anchor="w", text=f"{bearing:03.0f}°", fill=DIM, font=self.f(13, "bold"))

    def s_fuel(self, t, c):
        cv, k = self.cv, self.k
        ats = c["ats"]
        cap = t.fuelCapacity or 1
        fuel_frac = t.fuel / cap
        fc = RED if fuel_frac < (t.fuelWarningFactor or 0.15) else TXT
        amount = t.fuel * L_TO_GAL if ats else t.fuel
        self.dial(50, 46, 30, fuel_frac, 1, "", f"{amount:.0f}{'g' if ats else 'L'}", fc, ticks=4)
        cv.create_arc(k(20), k(16), k(80), k(76), start=210, extent=-30, style="arc", outline=RED, width=max(1, k(3)))
        cv.create_text(k(24), k(94), text="FUEL", fill=DIM, font=self.f(7))
        cv.create_text(k(52), k(94), text="DEF" if ats else "AdB", fill=DIM, font=self.f(7))
        dcap = t.adblueCapacity or 1
        dq = math.ceil(4 * t.adblue / dcap) if t.adblue > 0 else 0
        for i in range(4):
            on = i < dq
            cv.create_rectangle(k(66 + i * 9), k(90), k(73 + i * 9), k(98),
                                fill=(AMBER if dq <= 1 else BLUE) if on else "", outline=OFF)
        # average economy, and how much of the fuel on board the rest of the job needs
        # litres per km, as the game reports it. While driving it drops to 0 for a moment
        # between updates, which made the reading blink - hold the last real value instead.
        if t.fuelAvgConsumption > 0:
            self._last_lpk = t.fuelAvgConsumption
        lpk = getattr(self, "_last_lpk", 0.0)
        if t.fuelRange > 0:
            self._last_range = t.fuelRange
        if lpk > 0:
            econ = (1 / lpk) * 2.35215 if ats else lpk * 100   # mpg (US) or L/100km
            cv.create_text(k(144), k(30), text=f"{econ:.1f}", fill=TXT, font=self.f(13, "bold"))
        else:
            cv.create_text(k(144), k(30), text="–", fill=DIM, font=self.f(13, "bold"))
        cv.create_text(k(144), k(48), text="AVG MPG" if ats else "L/100KM", fill=DIM, font=self.f(7))
        # range, and whether it covers the rest of the job. Needing a stop on a long haul is
        # normal, so that is a steady amber "stop in", not an alarm; red only when nearly dry.
        rng_km = getattr(self, "_last_range", 0.0) or (t.fuel / lpk if lpk > 0 else 0)
        dist = (lambda km: km * 0.621371) if ats else (lambda km: km)
        unit = "mi" if ats else "km"
        margin = float(self.s.get("fuel_margin_km", 120))
        route_km = t.routeDistance / 1000
        if rng_km <= 0:
            val, lab, col = "–", "RANGE", DIM
        elif t.onJob and route_km > 0 and rng_km - margin < route_km:
            stop = max(0.0, rng_km - margin)
            val, lab = f"{dist(stop):,.0f} {unit}", "FUEL STOP IN"
            col = RED if rng_km < 100 else AMBER
        elif t.onJob and route_km > 0:
            val, lab, col = f"{dist(rng_km):,.0f} {unit}", "RANGE · MAKES IT", GREEN
        else:
            val, lab, col = f"{dist(rng_km):,.0f} {unit}", "RANGE", TXT
        cv.create_text(k(144), k(74), text=val, fill=col, font=self.f(12, "bold"))
        cv.create_text(k(144), k(92), text=lab, fill=DIM, font=self.f(7))

    def s_gauges(self, t, c):
        ats = c["ats"]
        temp = (lambda v: v * 9 / 5 + 32) if ats else (lambda v: v)
        deg = "°F" if ats else "°C"
        oil_t, water_t, brake_t = temp(t.oilTemperature), temp(t.waterTemperature), temp(t.brakeTemperature)
        tmax = 300 if ats else 150
        warn_v = t.batteryVoltageWarningLevel or 0
        air_col = RED if (t.airPressureWarning or t.airPressureEmergency) else TXT
        g = [
            # air pressure first, then oil pressure (moved out of the fuel tray)
            (t.airPressure, 0, 150, "AIR PSI", f"{t.airPressure:.0f}", air_col, None)
            if ats else
            (t.airPressure * PSI_TO_BAR, 0, 10, "AIR BAR", f"{t.airPressure * PSI_TO_BAR:.1f}", air_col, None),
            (t.oilPressure, 0, 100, "OIL PSI", f"{t.oilPressure:.0f}", RED if t.oilPressureWarning else TXT, None)
            if ats else
            (t.oilPressure * PSI_TO_BAR, 0, 7, "OIL BAR", f"{t.oilPressure * PSI_TO_BAR:.1f}",
             RED if t.oilPressureWarning else TXT, None),
            (oil_t, temp(40), tmax, "OIL " + deg, f"{oil_t:.0f}", TXT, tmax * 0.85),
            (water_t, temp(40), tmax, "WATER " + deg, f"{water_t:.0f}", RED if t.waterTemperatureWarning else TXT,
             temp(t.waterTemperatureWarningLevel) if t.waterTemperatureWarningLevel else None),
            (t.batteryVoltage, 0, 32 if t.batteryVoltage > 16 else 16, "BATT V", f"{t.batteryVoltage:.1f}",
             RED if t.batteryVoltageWarning else TXT, None),
            (brake_t, temp(0), temp(400), "BRAKE " + deg, f"{brake_t:.0f}",
             RED if t.brakeTemperature > 343 else (AMBER if t.brakeTemperature > 260 else TXT), temp(343)),
        ]
        for i, (v, lo, hi, lab, txt, col, red) in enumerate(g):
            self.dial(36 + i * 68, 46, 28, v, hi, lab, txt, col, red_from=red, vmin=lo)

    def s_job(self, t, c):
        cv, k = self.cv, self.k
        if not t.onJob:
            cv.create_text(k(140), k(56), text="No active job", fill=DIM, font=self.f(10))
            return
        right = 270                                    # inner edge of the card (width 280)
        dst = f"{T.text(t.cityDst)} - {T.text(t.compDst)}"
        f_dst = self.f(10, "bold")
        cv.create_text(k(10), k(22), anchor="w", text=self.fit(dst, f_dst, k(right - 10)), fill=TXT, font=f_dst)
        # weight pinned to the right edge; the cargo name gets whatever room is left, and
        # scrolls through it (pausing 2 s at the start of each pass) when it doesn't fit
        f_cargo, f_weight = self.f(9), self.f(9, "bold")
        weight, w_px = None, 0
        if t.cargoMass > 0:
            weight = f"{t.cargoMass * 2.20462:,.0f} lb" if c["ats"] else f"{t.cargoMass / 1000:,.1f} t"
            w_px = tkfont.Font(font=f_weight).measure(weight) + k(10)
        cargo = T.text(t.cargo)
        avail = k(right - 10) - w_px
        fm = tkfont.Font(font=f_cargo)
        if fm.measure(cargo) <= avail:
            cv.create_text(k(10), k(42), anchor="w", text=cargo, fill=DIM, font=f_cargo)
        else:
            if getattr(self, "_scroll", (None,))[0] != cargo:
                self._scroll = (cargo, time.time())
            loop = cargo + "      •      "
            loop_w = fm.measure(loop)
            speed, pause = k(35), 2.0                   # pixels per second, seconds held at the start
            tt = (time.time() - self._scroll[1]) % (pause + loop_w / speed)
            off = max(0.0, tt - pause) * speed
            # the canvas can't clip, so drop the characters already scrolled past and the
            # ones not yet in view; only a partial character at each end needs masking
            s = loop + cargo
            i = 0
            while i < len(s) and fm.measure(s[:i + 1]) <= off:
                i += 1
            x = k(10) - (off - fm.measure(s[:i]))
            s = s[i:]
            j = len(s)
            while j > 0 and fm.measure(s[:j]) > avail + k(12):
                j -= 1
            cv.create_text(x, k(42), anchor="w", text=s[:j], fill=DIM, font=f_cargo)
            cv.create_rectangle(k(1), k(32), k(10), k(53), fill=PANEL, outline="")
            cv.create_rectangle(k(10) + avail, k(32), k(right + 9), k(53), fill=PANEL, outline="")
        if weight:
            cv.create_text(k(right), k(42), anchor="e", text=weight, fill=TXT, font=f_weight)
        dist = t.routeDistance * (M_TO_MI if c["ats"] else 0.001)
        secs = max(0, int(t.routeTime))
        cv.create_text(k(10), k(72), anchor="w", text=f"{dist:,.0f} {'mi' if c['ats'] else 'km'}",
                       fill=TXT, font=self.f(14, "bold"))
        cv.create_text(k(144), k(72), anchor="w", text=f"{secs // 3600}h {secs % 3600 // 60:02d}m",
                       fill=TXT, font=self.f(14, "bold"))
        cv.create_text(k(10), k(94), anchor="w", text="ROUTE LEFT", fill=DIM, font=self.f(7))
        cv.create_text(k(144), k(94), anchor="w", text="TIME LEFT", fill=DIM, font=self.f(7))

    def s_weather(self, t, c):
        """The road ahead: the sky now, what's coming and when, and conditions on arrival."""
        cv, k = self.cv, self.k
        fc = self.forecast()
        if fc is None:
            return
        road = fc.get("road") or {}
        place = fc["place"].split(",")[0]
        if len(place) > 20:
            place = place[:19].rstrip() + "…"
        cv.create_text(k(10), k(22), anchor="w", text=f"{fc['date_label'].upper()}  ·  {place}",
                       fill=DIM, font=self.f(8, "bold"))
        if fc.get("alert"):
            cv.create_text(k(290), k(22), anchor="e", text=fc["alert"].split()[0].upper(), fill=AMBER,
                           font=self.f(8, "bold"))
        else:
            hi, lo = road.get("high", fc.get("high")), road.get("low", fc.get("low"))
            cv.create_text(k(290), k(22), anchor="e", text=f"H{hi}° L{lo}°", fill=DIM, font=self.f(8, "bold"))
        temp = road.get("temp", fc["now"]["temp"])
        cv.create_text(k(10), k(48), anchor="w", text=f"{temp}°", fill=TXT, font=self.f(20, "bold"))
        cv.create_text(k(78), k(48), anchor="w", text=road.get("now_sky", fc["now"]["sky"]), fill=TXT,
                       font=self.f(9), width=k(165))
        ahead = road.get("ahead", "")
        quiet = ahead.startswith("Dry for")
        lines = [(ahead, BLUE if road.get("wet_now") else (DIM if quiet else TXT)),
                 (road.get("later", ""), DIM)]
        for (text, col), y in zip(lines, (72, 92)):
            if not text:
                continue
            font = self.f(9 if y == 72 else 8)
            fm, avail = tkfont.Font(font=font), k(282)
            if fm.measure(text) > avail:                 # trim to fit the pane
                while text and fm.measure(text + "…") > avail:
                    text = text[:-1]
                text = text.rstrip() + "…"
            cv.create_text(k(10), k(y), anchor="w", text=text, fill=col, font=font)

    def clock_text(self, minutes):
        h24, mins = (minutes // 60) % 24, minutes % 60
        if self.s.get("clock_24h", False):
            return f"{h24:02d}:{mins:02d}"
        return f"{(h24 % 12) or 12}:{mins:02d} {'AM' if h24 < 12 else 'PM'}"

    def s_clock(self, t, c):
        cv, k = self.cv, self.k
        m = t.time_abs
        h24, mins = (m // 60) % 24, m % 60
        if self.s.get("clock_24h", False):
            cv.create_text(k(55), k(28), text=DAYS[(m // 1440) % 7], fill=DIM, font=self.f(9))
            cv.create_text(k(55), k(52), text=f"{h24:02d}:{mins:02d}", fill=TXT, font=self.f(16, "bold"))
        else:
            cv.create_text(k(55), k(26), text=f"{DAYS[(m // 1440) % 7]}  {'AM' if h24 < 12 else 'PM'}",
                           fill=DIM, font=self.f(9, "bold"))
            cv.create_text(k(55), k(52), text=f"{(h24 % 12) or 12}:{mins:02d}", fill=TXT, font=self.f(16, "bold"))
        rest = t.restStop
        if rest > 0:
            rc = GREEN if rest > 60 else AMBER
            cv.create_text(k(55), k(88), text=f"rest {rest // 60}h{rest % 60:02d}", fill=rc, font=self.f(8))


def main():
    demo = "ets2" if "--demo-ets2" in sys.argv else ("ats" if "--demo" in sys.argv else None)
    Bar(demo=demo, exit_with_game="--exit-with-game" in sys.argv)
    tk.mainloop()


if __name__ == "__main__":
    main()
