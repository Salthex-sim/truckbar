"""TruckBar's weather map: a pop-out national map of the next 24 game hours, written by a
separate weather program (wxmap.json) - rain, storms, snow and fog like a TV radar loop, highs
and lows with their fronts, city temperatures, weather alerts, your truck and your destination.

  Ctrl + Alt + W   open / close (works while driving; change it in TruckBar's settings)
  Drag             move it            Mouse wheel   step through the hours (pauses the loop)
  Click            play / pause       Ctrl + wheel  bigger / smaller
  Right-click      close

Like the bar, it never takes keyboard focus, so the game keeps it.

The picture is drawn with Pillow + numpy (Tk's canvas has no anti-aliasing or transparency):
the weather is the coarse grid resampled smooth and coloured on a continuous radar scale with
soft edges; borders, fronts and markers are drawn at twice the size and scaled down. Text
stays on the Tk canvas, which already draws it smooth.
"""
import ctypes
import datetime as dt
import json
import math
import os
import time
import tkinter as tk

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
except ImportError:                       # the map needs them; the bar itself doesn't
    np = None

HERE = os.path.dirname(os.path.abspath(__file__))
WXMAP = os.environ.get("TRUCKBAR_WXMAP") or os.path.join(os.environ.get("APPDATA", "."), "WeatherDirector", "wxmap.json")
FONT = "Segoe UI"
OCEAN, LAND, BORDER, COAST = (14, 21, 31), (31, 38, 49), (78, 88, 102, 150), (96, 108, 124, 220)
TXT, DIM, AMBER = "#e8e8e8", "#8a8f98", "#ffbf3f"
# radar: (intensity, colour) stops, coloured continuously between them; intensities are x10
RAIN_STOPS = [(1.0, (34, 120, 60)), (4.0, (47, 168, 79)), (7.0, (230, 200, 74)), (10.0, (232, 134, 42)),
              (13.0, (210, 58, 58)), (16.0, (190, 60, 170))]
SNOW_STOPS = [(1.0, (80, 118, 168)), (5.0, (150, 188, 230)), (9.0, (232, 240, 250))]
FOG_RGB = (120, 128, 140)
COLD, WARM = (77, 155, 255), (255, 90, 77)
LEGEND = [("Rain", "#2fa84f"), ("Heavy", "#e8862a"), ("Storms", "#d23a3a"), ("Snow", "#96bce6"), ("Fog", "#5c636e")]
ASPECT = 1.62
PLAY_S, HOLD_S = 0.6, 2.0             # seconds per hour of the loop, pause on the last hour
WEST, EAST, SOUTH, NORTH = -125.5, -66.0, 24.0, 49.6
SS = 2                                 # supersampling for lines and shapes
TOP_PAD, BOT_PAD = 34, 30


# Lambert conformal conic, standard parallels 33 and 45 N, centred 39 N 96 W (as the game's own map)
_P1, _P2, _P0, _L0 = (math.radians(v) for v in (33, 45, 39, -96))
_N = math.log(math.cos(_P1) / math.cos(_P2)) / math.log(math.tan(math.pi / 4 + _P2 / 2) / math.tan(math.pi / 4 + _P1 / 2))
_F = math.cos(_P1) * math.tan(math.pi / 4 + _P1 / 2) ** _N / _N
_R0 = _F / math.tan(math.pi / 4 + _P0 / 2) ** _N


def lcc(lon, lat):
    r = _F / math.tan(math.pi / 4 + math.radians(lat) / 2) ** _N
    th = _N * (math.radians(lon) - _L0)
    return r * math.sin(th), _R0 - r * math.cos(th)


def lcc_inverse(x, y):
    """numpy arrays of projected x, y -> (lon, lat) in degrees."""
    ry = _R0 - y
    r = np.sign(_N) * np.sqrt(x * x + ry * ry)
    lat = 2 * np.arctan((_F / np.maximum(r, 1e-9)) ** (1 / _N)) - math.pi / 2
    lon = _L0 + np.arctan2(x, ry) / _N
    return np.degrees(lon), np.degrees(lat)


def ramp(v, stops):
    """Intensity array -> RGB array, blended between the colour stops."""
    out = np.zeros(v.shape + (3,), np.float32)
    xs = [s for s, _ in stops]
    for ch in range(3):
        out[..., ch] = np.interp(v, xs, [c[ch] for _, c in stops])
    return out


def catmull(pts, steps=8):
    """A smooth curve through the points."""
    if len(pts) < 3:
        return pts
    p = [pts[0]] + pts + [pts[-1]]
    out = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        for s in range(steps):
            t = s / steps
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                                    + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3) for k in range(2)))
    out.append(pts[-1])
    return out


def along(path, every, offset):
    """Points (x, y, ux, uy) every `every` pixels along a polyline, starting `offset` in."""
    out, carry = [], every - offset
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        L = math.hypot(x1 - x0, y1 - y0)
        if L < 1e-6:
            continue
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        d = every - carry
        while d <= L:
            out.append((x0 + ux * d, y0 + uy * d, ux, uy))
            d += every
        carry = (carry + L) % every
    return out


def monitors():
    """[(label, (x, y, w, h))] for each screen, the main one first, the others named by where
    they sit relative to it (e.g. 'Above, left')."""
    import ctypes.wintypes as W

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]

    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)

    def cb(h, _dc, _r, _l):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(h, ctypes.byref(mi))
        r = mi.rcWork
        found.append(((r.left, r.top, r.right - r.left, r.bottom - r.top), bool(mi.dwFlags & 1)))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    main = next((r for r, p in found if p), found[0][0] if found else (0, 0, 1920, 1080))
    mx, my = main[0] + main[2] / 2, main[1] + main[3] / 2
    out = []
    for r, primary in sorted(found, key=lambda f: (not f[1], f[0][1], f[0][0])):
        if primary:
            name = "Main screen"
        else:
            cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
            v = "Above" if cy < my - main[3] / 2 else ("Below" if cy > my + main[3] / 2 else "")
            hz = "left" if cx < mx - r[2] / 4 else ("right" if cx > mx + r[2] / 4 else "")
            name = ", ".join(p for p in (v, hz) if p).capitalize() or "Other screen"
        out.append((f"{name}  ({r[2]}x{r[3]})", r))
    return out


CORNERS = ["Top left", "Top right", "Centre", "Bottom left", "Bottom right"]


def placed(rect, corner, width, margin=20):
    """(x, y, w) for a map of `width` in a corner of a screen's work area."""
    x0, y0, sw, sh = rect
    w = max(500, min(int(width), sw - 2 * margin))
    h = int(w / ASPECT)
    if h > sh - 2 * margin:
        h = sh - 2 * margin
        w = int(h * ASPECT)
    xs = {"left": x0 + margin, "right": x0 + sw - w - margin, "mid": x0 + (sw - w) // 2}
    ys = {"top": y0 + margin, "bottom": y0 + sh - h - margin, "mid": y0 + (sh - h) // 2}
    if corner == "Centre":
        return xs["mid"], ys["mid"], w
    v, hz = corner.lower().split()
    return xs[hz], ys[v], w


class MapWindow:
    def __init__(self, root, settings, save):
        self.s, self.save = settings, save
        self.top = tk.Toplevel(root)
        self.top.title("TruckBar weather map")
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.cv = tk.Canvas(self.top, bg="#0e151f", highlightthickness=1, highlightbackground="#2c3038", bd=0)
        self.cv.pack(fill="both", expand=True)
        g = self.s.get("map_geom") or [root.winfo_x(), root.winfo_y() + 200, 1000]
        self.x, self.y, self.w = int(g[0]), int(g[1]), int(g[2])
        self.top.geometry(f"{self.w}x{int(self.w / ASPECT)}+{self.x}+{self.y}")
        self.top.update_idletasks()
        self._no_focus()
        self.data, self.mtime, self.checked = None, 0, 0.0
        self.states = json.load(open(os.path.join(HERE, "us_states.json"), encoding="utf-8"))["states"]
        self.i, self.playing, self.next_at = 0, True, 0.0
        self.size = None                   # (w, h) the base layers were made for
        self.cache = {}                    # frame index -> rendered weather picture
        self.forecast, self.dest_city = None, ""
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-3>", lambda e: self.close())
        self.closed = False

    # -- window plumbing ------------------------------------------------------
    def _no_focus(self):
        u = ctypes.windll.user32
        hwnd = u.GetParent(self.top.winfo_id()) or self.top.winfo_id()
        style = u.GetWindowLongW(hwnd, -20)
        u.SetWindowLongW(hwnd, -20, (style | 0x08000000 | 0x00000080) & ~0x00040000)

    def _remember(self):
        self.s["map_geom"] = [self.x, self.y, self.w]
        self.save(self.s)

    def _press(self, e):
        self._grab = (e.x_root - self.x, e.y_root - self.y, e.x_root, e.y_root)

    def _drag(self, e):
        dx, dy = self._grab[:2]
        self.x, self.y = e.x_root - dx, e.y_root - dy
        self.top.geometry(f"+{self.x}+{self.y}")

    def _release(self, e):
        if abs(e.x_root - self._grab[2]) + abs(e.y_root - self._grab[3]) < 4:
            self.playing = not self.playing            # a click, not a drag
            self.next_at = time.time() + PLAY_S
            self.draw_frame()
        self._remember()

    def _wheel(self, e):
        step = 1 if e.delta > 0 else -1
        if e.state & 0x4:                                 # Ctrl: size
            self.w = max(500, min(2400, self.w + 60 * step))
            self.top.geometry(f"{self.w}x{int(self.w / ASPECT)}")
            self.top.update_idletasks()
            self._remember()
            self.draw_frame()
        elif self.data:
            self.playing = False
            self.i = (self.i + step) % len(self.data["frames"])
            self.draw_frame()

    def move(self, x, y, w):
        """Put the map somewhere new (from the settings window)."""
        self.x, self.y, self.w = int(x), int(y), int(w)
        self.top.geometry(f"{self.w}x{int(self.w / ASPECT)}+{self.x}+{self.y}")
        self.top.update_idletasks()
        self.draw_frame()

    def close(self):
        self._remember()
        self.top.destroy()
        self.closed = True

    # -- data -------------------------------------------------------------------
    def _load(self):
        now = time.time()
        if now - self.checked < 3:
            return False
        self.checked = now
        try:
            m = os.path.getmtime(WXMAP)
            if m == self.mtime:
                return False
            with open(WXMAP, encoding="utf-8") as f:
                data = json.load(f)
            self.mtime = m
            self.data = data if now - data.get("updated", 0) < 6 * 3600 and data.get("frames") else None
            self.i = min(self.i, len(self.data["frames"]) - 1) if self.data else 0
            self.cache.clear()
            return True
        except (OSError, ValueError):
            self.data = None
            return False

    # -- geometry -----------------------------------------------------------------
    def _fit(self, w, h):
        corners = [lcc(lon, lat) for lon in (WEST, -96, EAST) for lat in (SOUTH, NORTH)] + [lcc(-96, NORTH + 0.8)]
        xs, ys = zip(*corners)
        k = min((w - 20) / (max(xs) - min(xs)), (h - TOP_PAD - BOT_PAD) / (max(ys) - min(ys)))
        self._k = k
        self._cx = (w - k * (max(xs) - min(xs))) / 2 - k * min(xs)
        self._cy = TOP_PAD + (h - TOP_PAD - BOT_PAD - k * (max(ys) - min(ys))) / 2 + k * max(ys)

    def xy(self, lon, lat, scale=1):
        x, y = lcc(lon, lat)
        return (self._cx + self._k * x) * scale, (self._cy - self._k * y) * scale

    def _build_base(self, w, h):
        """Per window size: the land, the borders on top, a land mask, and where every pixel
        falls on the weather grid."""
        self._fit(w, h)
        big = Image.new("RGB", (w * SS, h * SS), OCEAN)
        mask = Image.new("L", (w * SS, h * SS), 0)
        d, dm = ImageDraw.Draw(big), ImageDraw.Draw(mask)
        rings = [[self.xy(lon, lat, SS) for lon, lat in r] for st in self.states for r in st["rings"]]
        for r in rings:
            d.polygon(r, fill=LAND)
            dm.polygon(r, fill=255)
        self.base = np.asarray(big.resize((w, h), Image.LANCZOS), np.float32)
        self.land = np.asarray(mask.resize((w, h), Image.LANCZOS), np.float32) / 255.0
        lines = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        dl = ImageDraw.Draw(lines)
        for r in rings:
            dl.line(r + r[:1], fill=BORDER, width=SS)
        self.borders = lines.resize((w, h), Image.LANCZOS)
        # grid coordinates of every pixel (fractional rows/columns of the weather grid)
        px, py = np.meshgrid(np.arange(w, dtype=np.float64) + 0.5, np.arange(h, dtype=np.float64) + 0.5)
        lon, lat = lcc_inverse((px - self._cx) / self._k, (self._cy - py) / self._k)
        self._lonlat = (lon, lat)
        self.size = (w, h)
        self.cache.clear()

    def _sample(self, flat, lats, lons, up=6):
        """A 1-degree field (row-major, north to south) as a smooth picture-sized array."""
        nlat, nlon = len(lats), len(lons)
        # x10 in an 8-bit picture (values run 0..~25), so Pillow can resample and soften it
        g = np.clip(np.asarray(flat, np.float32).reshape(nlat, nlon) * 10, 0, 255).astype(np.uint8)
        img = Image.fromarray(g, "L").resize((nlon * up, nlat * up), Image.BICUBIC)
        img = img.filter(ImageFilter.GaussianBlur(up * 0.35))
        fine = np.asarray(img, np.float32) / 10
        lon, lat = self._lonlat
        # grid point j sits at the centre of fine pixels j*up .. j*up+up-1
        fx = ((lon - lons[0]) / (lons[1] - lons[0]) + 0.5) * up - 0.5
        fy = ((lat - lats[0]) / (lats[1] - lats[0]) + 0.5) * up - 0.5
        # blended between the four nearest fine pixels, so edges don't step
        x0 = np.clip(np.floor(fx).astype(int), 0, nlon * up - 2)
        y0 = np.clip(np.floor(fy).astype(int), 0, nlat * up - 2)
        tx, ty = np.clip(fx - x0, 0, 1), np.clip(fy - y0, 0, 1)
        out = ((fine[y0, x0] * (1 - tx) + fine[y0, x0 + 1] * tx) * (1 - ty)
               + (fine[y0 + 1, x0] * (1 - tx) + fine[y0 + 1, x0 + 1] * tx) * ty)
        # fade out over the last grid cell and a half, so weather doesn't stop in a straight line
        edge = np.minimum.reduce([fx, nlon * up - 1 - fx, fy, nlat * up - 1 - fy]) / (1.5 * up)
        return np.maximum(out, 0) * np.clip(edge, 0, 1)

    def _weather_picture(self, f):
        d = self.data
        lats, lons = d["lats"], d["lons"]
        rain, snow, fog = (self._sample(f[k], lats, lons) for k in ("rain", "snow", "fog"))
        img = self.base.copy()
        # fog first (land only, soft and see-through), then the precipitation over it
        a = np.clip((fog - 0.25) / 0.5, 0, 1) * 0.42 * self.land
        img = img * (1 - a[..., None]) + np.array(FOG_RGB, np.float32) * a[..., None]
        is_snow = snow >= np.maximum(0.8, rain * 0.5)
        v = np.where(is_snow, snow, rain)
        col = np.where(is_snow[..., None], ramp(v, SNOW_STOPS), ramp(v, RAIN_STOPS))
        a = np.clip((v - 0.9) / 0.7, 0, 1) * 0.86          # a crisp radar-style outer edge
        img = img * (1 - a[..., None]) + col * a[..., None]
        pic = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
        pic.alpha_composite(self.borders)
        pic.alpha_composite(self._fronts(f.get("fronts", [])))
        return pic

    def _fronts(self, fronts):
        w, h = self.size
        lay = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        lw, s = int(3 * SS), 7 * SS
        for fr in fronts:
            path = catmull([self.xy(lon, lat, SS) for lon, lat in fr["pts"]])
            kind = fr["type"]
            if kind == "stationary":                     # alternating cold and warm, symbols on both sides
                pieces = along(path, 14 * SS, 0)
                for n, ((x0, y0, *_), (x1, y1, *_)) in enumerate(zip(pieces, pieces[1:])):
                    d.line([(x0, y0), (x1, y1)], fill=COLD if n % 2 == 0 else WARM, width=lw)
                continue
            col = COLD if kind == "cold" else WARM
            d.line(path, fill=col, width=lw, joint="curve")
            for x, y, ux, uy in along(path, 30 * SS, 15 * SS):
                nx, ny = uy, -ux
                if (kind == "cold" and nx < 0) or (kind == "warm" and ny > 0):
                    nx, ny = -nx, -ny                    # cold fronts push east, warm fronts north
                if kind == "cold":
                    d.polygon([(x - ux * s, y - uy * s), (x + ux * s, y + uy * s), (x + nx * s * 1.5, y + ny * s * 1.5)], fill=col)
                else:
                    ang = math.degrees(math.atan2(ny, nx))
                    d.pieslice([x - s, y - s, x + s, y + s], ang - 90, ang + 90, fill=col)
        return lay.resize((w, h), Image.LANCZOS)

    def _sprites(self, k):
        """The truck and destination markers, anti-aliased."""
        if getattr(self, "_sprite_k", None) == k:
            return
        r = max(6, int(8 * k))
        n = (2 * r + 4) * SS
        im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        c = n / 2
        d.ellipse([c - r * SS, c - r * SS, c + r * SS, c + r * SS], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=2 * SS)
        d.ellipse([c - r * SS * 0.4, c - r * SS * 0.4, c + r * SS * 0.4, c + r * SS * 0.4], fill=(0, 0, 0, 255))
        self._truck_img = ImageTk.PhotoImage(im.resize((n // SS, n // SS), Image.LANCZOS))
        q = max(5, int(6 * k))
        n = (2 * q + 4) * SS
        im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        c = n / 2
        d.rounded_rectangle([c - q * SS, c - q * SS, c + q * SS, c + q * SS], radius=2 * SS, fill=(255, 191, 63, 255),
                            outline=(0, 0, 0, 255), width=SS)
        self._dest_img = ImageTk.PhotoImage(im.resize((n // SS, n // SS), Image.LANCZOS))
        self._sprite_k = k

    # -- drawing --------------------------------------------------------------------
    def tick(self, forecast, dest_city=""):
        """Called from the bar's loop; redraws when the hour changes or new data arrives."""
        if self.closed:
            return
        self.forecast, self.dest_city = forecast, dest_city
        fresh = self._load()
        now = time.time()
        if self.data and self.playing and now >= self.next_at:
            last = len(self.data["frames"]) - 1
            self.i = 0 if self.i >= last else self.i + 1
            self.next_at = now + (HOLD_S if self.i == last else PLAY_S)
            self.draw_frame()
        elif fresh or self.size is None:
            self.draw_frame()

    def draw_frame(self):
        cv = self.cv
        w, h = self.top.winfo_width(), self.top.winfo_height()
        cv.delete("all")
        if np is None:
            cv.create_text(w / 2, h / 2, text="The weather map needs Pillow and numpy:\npip install pillow numpy",
                           fill=DIM, font=(FONT, 12), justify="center")
            return
        if self.size != (w, h):
            self._build_base(w, h)
        if not self.data:
            self._photo = ImageTk.PhotoImage(Image.fromarray(self.base.astype(np.uint8), "RGB"))
            cv.create_image(0, 0, anchor="nw", image=self._photo)
            cv.create_text(w / 2, h / 2, text="No weather map yet - it appears once the weather director\n"
                                              "is running alongside the game.", fill=DIM, font=(FONT, 12), justify="center")
            return
        f = self.data["frames"][self.i]
        if self.i not in self.cache:
            self.cache[self.i] = ImageTk.PhotoImage(self._weather_picture(f))
        cv.create_image(0, 0, anchor="nw", image=self.cache[self.i])
        k = max(0.7, min(1.6, w / 1100))
        for p in f.get("highs", []) + f.get("lows", []):
            x, y = self.xy(p["lon"], p["lat"])
            col = "#4d9bff" if p["kind"] == "H" else "#ff5a4d"
            cv.create_text(x, y, text=p["kind"], fill=col, font=(FONT, int(22 * k), "bold"))
            cv.create_text(x, y + 20 * k, text=str(p["mb"]), fill=col, font=(FONT, int(8 * k)))
        for c, temp in zip(self.data["cities"], f.get("temps", [])):
            x, y = self.xy(c["lon"], c["lat"])
            cv.create_text(x + 1, y, text="•", fill=DIM, font=(FONT, int(8 * k)))
            cv.create_text(x + 6, y - 1, anchor="w", text=f"{temp}°", fill=TXT, font=(FONT, int(9 * k), "bold"))
            if w >= 900:
                cv.create_text(x + 6, y + 11 * k, anchor="w", text=c["name"], fill=DIM, font=(FONT, int(7 * k)))
        for e in f.get("events", []):
            x, y = self.xy(e["lon"], e["lat"])
            cv.create_text(x, y, text="⚠ " + e["label"], fill=AMBER, font=(FONT, int(9 * k), "bold"))
        self._truck(k)
        self._header(w, h, f)

    def _truck(self, k):
        fc = self.forecast or {}
        if "lat" not in fc:
            return
        cv = self.cv
        self._sprites(k)
        x, y = self.xy(fc["lon"], fc["lat"])
        dest = fc.get("dest")
        if dest:
            dx, dy = self.xy(dest[1], dest[0])
            cv.create_line(x, y, dx, dy, fill=TXT, width=2, dash=(6, 5))
            cv.create_image(dx, dy, image=self._dest_img)
            if self.dest_city:
                cv.create_text(dx, dy - 10 * k, anchor="s", text=self.dest_city, fill=AMBER, font=(FONT, int(9 * k), "bold"))
        cv.create_image(x, y, image=self._truck_img)

    def _header(self, w, h, f):
        cv = self.cv
        hour = f["hour"]
        when = f"{(hour % 12) or 12} {'AM' if hour < 12 else 'PM'}"
        day = dt.date.fromisoformat(f["date"]).strftime("%a %b %d").replace(" 0", " ")
        ahead = "NOW" if self.i == 0 else f"+{self.i} h"
        cv.create_rectangle(0, 0, w, 30, fill="#15171b", outline="")
        cv.create_text(12, 15, anchor="w", text=f"WEATHER  ·  {day}  {when}", fill=TXT, font=(FONT, 11, "bold"))
        cv.create_text(w - 12, 15, anchor="e", text=("▶ " if self.playing else "❚❚ ") + ahead,
                       fill=AMBER if self.i == 0 else TXT, font=(FONT, 11, "bold"))
        n = len(self.data["frames"])                     # hour ticks, the shown hour lit
        x0, x1, yb = 12, w * 0.55, h - 14
        for j in range(n):
            x = x0 + (x1 - x0) * j / max(1, n - 1)
            cv.create_line(x, yb - (7 if j == self.i else 3), x, yb + 3, fill=AMBER if j == self.i else DIM,
                           width=3 if j == self.i else 1)
        x = w - 12
        for label, c in reversed(LEGEND):
            cv.create_text(x, yb, anchor="e", text=label, fill=DIM, font=(FONT, 8))
            x -= len(label) * 7 + 6
            cv.create_rectangle(x - 10, yb - 5, x, yb + 5, fill=c, outline="")
            x -= 18
