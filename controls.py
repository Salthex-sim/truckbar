"""Watches the player's own wiper controls, so TruckBar can show what the telemetry can't:
the wiper *speed* (the telemetry only says wipers on/off). Also reads whether automatic
high beams are switched on in the game's options.

The bindings come from the profile's controls.sii - whatever keys or wheel/joystick
buttons that player has set for:
    wipers       wipers: next speed
    wipersback   wipers: previous speed
Keyboard keys are read with GetAsyncKeyState and game-controller buttons with the winmm
joystick API, matched to the game's devices by USB VID/PID. Neither needs focus.
A background thread polls at 100 Hz and counts presses; the bar takes the counts.
"""
import ctypes
import ctypes.wintypes as W
import os
import re
import threading
import time

ACTIONS = ("wipers", "wipersback", "trailerbrake")

_VK = {"space": 0x20, "enter": 0x0D, "numenter": 0x0D, "tab": 0x09, "esc": 0x1B, "back": 0x08,
       "backspace": 0x08, "del": 0x2E, "ins": 0x2D, "home": 0x24, "end": 0x23, "pgup": 0x21,
       "pgdn": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
       "lbracket": 0xDB, "rbracket": 0xDD, "semicolon": 0xBA, "apostrophe": 0xDE, "comma": 0xBC,
       "period": 0xBE, "slash": 0xBF, "backslash": 0xDC, "minus": 0xBD, "equals": 0xBB,
       "grave": 0xC0, "numadd": 0x6B, "numsub": 0x6D, "nummul": 0x6A, "numdiv": 0x6F,
       "numdot": 0x6E, "capslock": 0x14}


def vk_for(name):
    n = name.lower()
    if len(n) == 1 and n.isalpha():
        return ord(n.upper())
    if len(n) == 1 and n.isdigit():
        return ord(n)
    m = re.fullmatch(r"f(\d{1,2})", n)
    if m and 1 <= int(m.group(1)) <= 24:
        return 0x6F + int(m.group(1))
    m = re.fullmatch(r"num(\d)", n)
    if m:
        return 0x60 + int(m.group(1))
    return _VK.get(n)


class JOYCAPSW(ctypes.Structure):
    _fields_ = [("wMid", W.WORD), ("wPid", W.WORD), ("szPname", W.WCHAR * 32),
                ("wXmin", W.UINT), ("wXmax", W.UINT), ("wYmin", W.UINT), ("wYmax", W.UINT),
                ("wZmin", W.UINT), ("wZmax", W.UINT), ("wNumButtons", W.UINT),
                ("wPeriodMin", W.UINT), ("wPeriodMax", W.UINT), ("wRmin", W.UINT), ("wRmax", W.UINT),
                ("wUmin", W.UINT), ("wUmax", W.UINT), ("wVmin", W.UINT), ("wVmax", W.UINT),
                ("wCaps", W.UINT), ("wMaxAxes", W.UINT), ("wNumAxes", W.UINT), ("wMaxButtons", W.UINT),
                ("szRegKey", W.WCHAR * 32), ("szOEMVxD", W.WCHAR * 260)]


class JOYINFOEX(ctypes.Structure):
    _fields_ = [("dwSize", W.DWORD), ("dwFlags", W.DWORD), ("dwXpos", W.DWORD), ("dwYpos", W.DWORD),
                ("dwZpos", W.DWORD), ("dwRpos", W.DWORD), ("dwUpos", W.DWORD), ("dwVpos", W.DWORD),
                ("dwButtons", W.DWORD), ("dwButtonNumber", W.DWORD), ("dwPOV", W.DWORD),
                ("dwReserved1", W.DWORD), ("dwReserved2", W.DWORD)]


_winmm = ctypes.WinDLL("winmm")
_user32 = ctypes.WinDLL("user32")
JOY_RETURNBUTTONS = 0x80


def profile_dir(documents, game):
    """The profile in use: the one whose config_local.cfg changed most recently."""
    root = os.path.join(documents, "Euro Truck Simulator 2" if game == 1 else "American Truck Simulator")
    best = None
    for sub in ("steam_profiles", "profiles"):
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for p in os.listdir(base):
            d = os.path.join(base, p)
            c = os.path.join(d, "controls.sii")
            if os.path.isfile(c):
                stamp = os.path.getmtime(os.path.join(d, "config_local.cfg")) if os.path.isfile(
                    os.path.join(d, "config_local.cfg")) else os.path.getmtime(c)
                if best is None or stamp > best[0]:
                    best = (stamp, d)
    return best[1] if best else None


def game_options(documents, game, names, default):
    """{name: on?} for on/off options in the game's settings, e.g. g_automatic_high_beams
    ("Automatic high beams") or g_fatigue ("Fatigue simulation"). The game logs each one when the
    profile loads and again whenever it changes, so the last mention is current."""
    log = os.path.join(documents, "Euro Truck Simulator 2" if game == 1 else "American Truck Simulator",
                       "game.log.txt")
    try:
        txt = open(log, encoding="utf-8", errors="replace").read()
    except OSError:
        return {n: default[n] for n in names}
    out = {}
    for n in names:
        found = re.findall(rf'uset {n} "(\d)"', txt)
        out[n] = found[-1] == "1" if found else default[n]
    return out


def read_bindings(controls_path):
    """-> {action: {"keys": [vk...], "buttons": [(vid, pid, button_index)...]}}"""
    txt = open(controls_path, encoding="utf-8", errors="replace").read()
    devices = {}
    for m in re.finditer(r'"device (\w+) `di8\.\'\{[^}]*\}\|\{([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})-', txt):
        devices[m.group(1)] = (int(m.group(3), 16), int(m.group(2), 16))     # (vid, pid)
    out = {}
    for action in ACTIONS:
        m = re.search(r'"mix ' + action + r' `([^`]*)`"', txt)
        keys, buttons = [], []
        if m:
            for dev, inp in re.findall(r"(\w+)\.(\w+)\?0", m.group(1)):
                if dev == "keyboard":
                    vk = vk_for(inp)
                    if vk:
                        keys.append(vk)
                elif dev in devices:
                    b = re.fullmatch(r"b(\d+)", inp)
                    if b and 1 <= int(b.group(1)) <= 32:
                        buttons.append(devices[dev] + (int(b.group(1)) - 1,))
        out[action] = {"keys": keys, "buttons": buttons}
    return out


def joystick_ids():
    """(vid, pid) -> winmm joystick id, for every connected joystick."""
    ids = {}
    caps = JOYCAPSW()
    for jid in range(16):
        if _winmm.joyGetDevCapsW(jid, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            ids.setdefault((caps.wMid, caps.wPid), jid)
    return ids


class ControlWatcher(threading.Thread):
    """Counts presses of the player's wiper controls."""

    def __init__(self, documents_folder, game_getter):
        super().__init__(daemon=True)
        self.documents = documents_folder
        self.game_getter = game_getter          # returns 1 (ETS2) / 2 (ATS) / None
        self.lock = threading.Lock()
        self.counts = {a: 0 for a in ACTIONS}
        self.held = {a: False for a in ACTIONS}  # physically down right now
        self.bindings = {}
        self.summary = "no bindings read yet"
        self.auto_high_beams = False
        self.fatigue = True                      # until the log says otherwise, show the rest timer

    def take(self):
        """Presses since the last call, per action."""
        with self.lock:
            c = dict(self.counts)
            for a in ACTIONS:
                self.counts[a] = 0
        return c

    def _reload(self):
        game = self.game_getter() or 2
        d = profile_dir(self.documents, game)
        if not d:
            self.bindings, self.summary = {}, "no profile found"
            return
        self.bindings = read_bindings(os.path.join(d, "controls.sii"))
        self.joy = joystick_ids()
        parts = [f"{a}: {len(b['keys'])} key(s), {len(b['buttons'])} button(s)" for a, b in self.bindings.items()]
        self.summary = "; ".join(parts)

    def run(self):
        self.joy = {}
        last_reload = last_auto = 0.0
        down = {a: False for a in ACTIONS}
        info = JOYINFOEX()
        info.dwSize = ctypes.sizeof(info)
        info.dwFlags = JOY_RETURNBUTTONS
        while True:
            now = time.time()
            if now - last_reload > 30:          # pick up rebinds and replugged devices
                try:
                    self._reload()
                except Exception as ex:
                    self.summary = f"bindings error: {ex}"
                last_reload = now
            if now - last_auto > 5:             # picks up a change in the game's options
                opts = game_options(self.documents, self.game_getter() or 2, ("g_automatic_high_beams", "g_fatigue"),
                                    {"g_automatic_high_beams": False, "g_fatigue": True})
                self.auto_high_beams, self.fatigue = opts["g_automatic_high_beams"], opts["g_fatigue"]
                last_auto = now
            buttons_by_joy = {}
            for action, b in self.bindings.items():
                pressed = any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in b["keys"])
                for vid, pid, idx in b["buttons"]:
                    jid = self.joy.get((vid, pid))
                    if jid is None:
                        continue
                    if jid not in buttons_by_joy:
                        buttons_by_joy[jid] = info.dwButtons if _winmm.joyGetPosEx(jid, ctypes.byref(info)) == 0 else 0
                    if buttons_by_joy[jid] & (1 << idx):
                        pressed = True
                if pressed and not down[action]:
                    with self.lock:
                        self.counts[action] += 1
                down[action] = pressed
                self.held[action] = pressed
            time.sleep(0.01)
