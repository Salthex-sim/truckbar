"""TruckBar's weather map: a pop-out national map of the next 24 game hours, written by a
separate weather program (wxmap.json) - rain, storms, snow and fog like a TV radar loop, highs
and lows with their fronts, city temperatures, weather alerts, your truck and your destination.

  Ctrl + Alt + W   open / close (works while driving)
  Drag             move it            Mouse wheel   step through the hours (pauses the loop)
  Click            play / pause       Ctrl + wheel  bigger / smaller
  Right-click      close

Like the bar, it never takes keyboard focus, so the game keeps it.
"""
import ctypes
import json
import math
import os
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
WXMAP = os.environ.get("TRUCKBAR_WXMAP") or os.path.join(os.environ.get("APPDATA", "."), "WeatherDirector", "wxmap.json")
FONT = "Segoe UI"
OCEAN, LAND, BORDER = "#0f1722", "#1d2530", "#47515f"
TXT, DIM, AMBER = "#e8e8e8", "#8a8f98", "#ffbf3f"
RAIN = [(13, "#d23a3a"), (10, "#e8862a"), (7, "#e6c84a"), (4, "#2fa84f"), (1, "#1f6f3a")]   # radar, heavy first
SNOW = [(9, "#e4edf8"), (5, "#9cbde3"), (1, "#5a7fa8")]
FOG = "#3d434c"
COLD, WARM = "#4d9bff", "#ff5a4d"
ASPECT = 1.62
PLAY_S, HOLD_S = 0.6, 2.0             # seconds per hour of the loop, pause on the last hour
WEST, EAST, SOUTH, NORTH = -125.5, -66.0, 24.0, 49.6


# Lambert conformal conic, standard parallels 33 and 45 N, centred 39 N 96 W (as the game's own map)
_P1, _P2, _P0, _L0 = (math.radians(v) for v in (33, 45, 39, -96))
_N = math.log(math.cos(_P1) / math.cos(_P2)) / math.log(math.tan(math.pi / 4 + _P2 / 2) / math.tan(math.pi / 4 + _P1 / 2))
_F = math.cos(_P1) * math.tan(math.pi / 4 + _P1 / 2) ** _N / _N
_R0 = _F / math.tan(math.pi / 4 + _P0 / 2) ** _N


def lcc(lon, lat):
    r = _F / math.tan(math.pi / 4 + math.radians(lat) / 2) ** _N
    th = _N * (math.radians(lon) - _L0)
    return r * math.sin(th), _R0 - r * math.cos(th)


def colour(steps, v):
    for lim, c in steps:
        if v >= lim:
            return c
    return None


class MapWindow:
    def __init__(self, root, settings, save):
        self.s, self.save = settings, save
        self.top = tk.Toplevel(root)
        self.top.title("TruckBar weather map")
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.cv = tk.Canvas(self.top, bg=OCEAN, highlightthickness=1, highlightbackground="#2c3038", bd=0)
        self.cv.pack(fill="both", expand=True)
        g = self.s.get("map_geom") or [root.winfo_x(), root.winfo_y() + 200, 1000]
        self.x, self.y, self.w = int(g[0]), int(g[1]), int(g[2])
        self.top.geometry(f"{self.w}x{int(self.w / ASPECT)}+{self.x}+{self.y}")
        self.top.update_idletasks()
        self._no_focus()
        self.data, self.mtime, self.checked = None, 0, 0.0
        self.states = json.load(open(os.path.join(HERE, "us_states.json"), encoding="utf-8"))["states"]
        self.i, self.playing, self.next_at, self.wheel_at = 0, True, 0.0, 0.0
        self.base_drawn_for = None
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
            self.base_drawn_for = None
            self._remember()
        elif self.data:
            self.playing = False
            self.i = (self.i + step) % len(self.data["frames"])
            self.draw_frame()

    def close(self):
        self._remember()
        self.top.destroy()
        self.closed = True

    # -- data -------------------------------------------------------------------
    def _load(self):
        now = time.time()
        if now - self.checked < 3:
            return
        self.checked = now
        try:
            m = os.path.getmtime(WXMAP)
            if m != self.mtime:
                with open(WXMAP, encoding="utf-8") as f:
                    data = json.load(f)
                self.mtime = m
                self.data = data if now - data.get("updated", 0) < 6 * 3600 and data.get("frames") else None
                self.i = min(self.i, len(self.data["frames"]) - 1) if self.data else 0
        except (OSError, ValueError):
            self.data = None

    # -- the half-degree grid ----------------------------------------------------------
    @staticmethod
    def _fine_axes(lats, lons):
        fl = [lats[0] + (lats[1] - lats[0]) * i / 2 for i in range(2 * len(lats) - 1)]
        fo = [lons[0] + (lons[1] - lons[0]) * i / 2 for i in range(2 * len(lons) - 1)]
        return fl, fo

    @staticmethod
    def _fine(flat, lats, lons):
        """A 1-degree field (row-major) eased onto the half-degree grid (bilinear)."""
        nlat, nlon = len(lats), len(lons)
        g = [flat[a * nlon:(a + 1) * nlon] for a in range(nlat)]
        out = []
        for i in range(2 * nlat - 1):
            a0, a1 = i // 2, (i + 1) // 2
            row = []
            for j in range(2 * nlon - 1):
                b0, b1 = j // 2, (j + 1) // 2
                row.append((g[a0][b0] + g[a0][b1] + g[a1][b0] + g[a1][b1]) / 4)
            out.append(row)
        return out

    def _land(self, flats, flons):
        """Which half-degree points are inside a state (worked out once)."""
        key = (len(flats), len(flons))
        if getattr(self, "_land_key", None) == key:
            return self._land_mask
        rings = []
        for st in self.states:
            for r in st["rings"]:
                xs, ys = [p[0] for p in r], [p[1] for p in r]
                rings.append((min(xs), max(xs), min(ys), max(ys), r))

        def inside(lon, lat):
            for x0, x1, y0, y1, r in rings:
                if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                    continue
                hit = False
                for (ax, ay), (bx, by) in zip(r, r[1:] + r[:1]):
                    if (ay > lat) != (by > lat) and lon < ax + (lat - ay) * (bx - ax) / (by - ay):
                        hit = not hit
                if hit:
                    return True
            return False

        self._land_mask = [[inside(lon, lat) for lon in flons] for lat in flats]
        self._land_key = key
        return self._land_mask

    # -- geometry -----------------------------------------------------------------
    def _fit(self, w, h):
        xs, ys = zip(*(lcc(lon, lat) for lon in (WEST, -96, EAST) for lat in (SOUTH, NORTH)))
        xs, ys = list(xs) + [lcc(-96, NORTH + 0.8)[0]], list(ys) + [lcc(-96, NORTH + 0.8)[1]]
        top_pad, bot_pad = 34, 30
        k = min((w - 20) / (max(xs) - min(xs)), (h - top_pad - bot_pad) / (max(ys) - min(ys)))
        cx = (w - k * (max(xs) - min(xs))) / 2 - k * min(xs)
        cy = top_pad + (h - top_pad - bot_pad - k * (max(ys) - min(ys))) / 2 + k * max(ys)
        self._k, self._cx, self._cy = k, cx, cy

    def xy(self, lon, lat):
        x, y = lcc(lon, lat)
        return self._cx + self._k * x, self._cy - self._k * y

    # -- drawing --------------------------------------------------------------------
    def tick(self, forecast, dest_city=""):
        """Called from the bar's loop; redraws when the hour changes or new data arrives."""
        if self.closed:
            return
        self.forecast, self.dest_city = forecast, dest_city
        old = self.mtime
        self._load()
        now = time.time()
        if self.data and self.playing and now >= self.next_at:
            last = len(self.data["frames"]) - 1
            self.i = 0 if self.i >= last else self.i + 1
            self.next_at = now + (HOLD_S if self.i == last else PLAY_S)
            self.draw_frame()
        elif self.mtime != old or self.base_drawn_for is None:
            self.draw_frame()

    def draw_base(self, w, h):
        cv = self.cv
        cv.delete("all")
        self._fit(w, h)
        for st in self.states:
            for ring in st["rings"]:
                cv.create_polygon([c for lon, lat in ring for c in self.xy(lon, lat)], fill=LAND, outline="", tags="land")
        self.base_drawn_for = (w, h)

    def draw_frame(self):
        cv = self.cv
        w, h = self.top.winfo_width(), self.top.winfo_height()
        if self.base_drawn_for != (w, h):
            self.draw_base(w, h)
        cv.delete("wx")
        if not self.data:
            cv.create_text(w / 2, h / 2, text="No weather map yet - it appears once the weather director\n"
                                              "is running alongside the game.", fill=DIM, font=(FONT, 12),
                           justify="center", tags="wx")
            return
        d, f = self.data, self.data["frames"][self.i]
        # radar on a half-degree grid, eased between the 1-degree points; fog only over land
        lats, lons = d["lats"], d["lons"]
        rain, snow, fog = (self._fine(f[k], lats, lons) for k in ("rain", "snow", "fog"))
        flats, flons = self._fine_axes(lats, lons)
        land = self._land(flats, flons)
        for a, lat in enumerate(flats):
            for b, lon in enumerate(flons):
                s_, r_ = snow[a][b], rain[a][b]
                c = colour(SNOW, s_) if s_ >= max(1, r_ * 0.5) else colour(RAIN, r_)
                stip = ""
                if c is None and fog[a][b] >= 0.5 and land[a][b]:
                    c, stip = FOG, "gray50"
                if c is None:
                    continue
                pts = [self.xy(lon + dx, lat + dy) for dx, dy in ((-.25, -.25), (.25, -.25), (.25, .25), (-.25, .25))]
                cv.create_polygon([v for p in pts for v in p], fill=c, outline="" if stip else c, stipple=stip, tags="wx")
        # state lines over the radar
        for st in self.states:
            for ring in st["rings"]:
                cv.create_line([c for lon, lat in ring + ring[:1] for c in self.xy(lon, lat)], fill=BORDER, tags="wx")
        self._fronts(f.get("fronts", []))
        k = max(0.7, min(1.6, w / 1100))
        for p in f.get("highs", []) + f.get("lows", []):
            x, y = self.xy(p["lon"], p["lat"])
            col = COLD if p["kind"] == "H" else WARM
            cv.create_text(x, y, text=p["kind"], fill=col, font=(FONT, int(22 * k), "bold"), tags="wx")
            cv.create_text(x, y + 20 * k, text=str(p["mb"]), fill=col, font=(FONT, int(8 * k)), tags="wx")
        for c, temp in zip(d["cities"], f.get("temps", [])):
            x, y = self.xy(c["lon"], c["lat"])
            cv.create_oval(x - 2, y - 2, x + 2, y + 2, fill=DIM, outline="", tags="wx")
            cv.create_text(x + 5, y - 1, anchor="w", text=f"{temp}°", fill=TXT, font=(FONT, int(9 * k), "bold"), tags="wx")
            if w >= 900:
                cv.create_text(x + 5, y + 11 * k, anchor="w", text=c["name"], fill=DIM, font=(FONT, int(7 * k)), tags="wx")
        for e in f.get("events", []):
            x, y = self.xy(e["lon"], e["lat"])
            cv.create_text(x, y, text="⚠ " + e["label"], fill=AMBER, font=(FONT, int(9 * k), "bold"), tags="wx")
        self._truck(k)
        self._header(w, h, f, k)

    def _fronts(self, fronts):
        cv = self.cv
        for fr in fronts:
            pts = [self.xy(lon, lat) for lon, lat in fr["pts"]]
            flat = [v for p in pts for v in p]
            if fr["type"] == "stationary":       # alternating cold and warm pieces (Tk's dashes can't offset)
                piece, n = 16.0, 0
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    L = math.hypot(x1 - x0, y1 - y0)
                    m = max(1, int(L // piece))
                    for q in range(m):
                        a, b = q / m, (q + 1) / m
                        cv.create_line(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a, x0 + (x1 - x0) * b, y0 + (y1 - y0) * b,
                                       fill=COLD if n % 2 == 0 else WARM, width=3, tags="wx")
                        n += 1
                continue
            col = COLD if fr["type"] == "cold" else WARM
            cv.create_line(flat, fill=col, width=3, smooth=True, tags="wx")
            # symbols on the side the front is heading for: triangles (cold), half-discs (warm)
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                L = math.hypot(x1 - x0, y1 - y0)
                if L < 1:
                    continue
                ux, uy = (x1 - x0) / L, (y1 - y0) / L
                nx, ny = uy, -ux
                if (fr["type"] == "cold" and nx < 0) or (fr["type"] == "warm" and ny > 0):
                    nx, ny = -nx, -ny                    # cold fronts push east, warm fronts north (up)
                s = 7
                for m in range(1, int(L // 28) + 1):
                    mx, my = x0 + ux * (m * 28 - 14), y0 + uy * (m * 28 - 14)
                    if fr["type"] == "cold":
                        cv.create_polygon(mx - ux * s, my - uy * s, mx + ux * s, my + uy * s,
                                          mx + nx * s * 1.4, my + ny * s * 1.4, fill=col, outline=col, tags="wx")
                    else:                                # a half-disc bulging ahead of the front
                        ang = math.degrees(math.atan2(-ny, nx))
                        cv.create_arc(mx - s, my - s, mx + s, my + s, start=ang - 90, extent=180,
                                      fill=col, outline=col, style="pieslice", tags="wx")

    def _truck(self, k):
        fc = self.forecast or {}
        if "lat" not in fc:
            return
        cv = self.cv
        x, y = self.xy(fc["lon"], fc["lat"])
        dest = fc.get("dest")
        if dest:
            dx, dy = self.xy(dest[1], dest[0])
            cv.create_line(x, y, dx, dy, fill=TXT, width=2, dash=(6, 5), tags="wx")
            r = 6 * k
            cv.create_rectangle(dx - r, dy - r, dx + r, dy + r, fill=AMBER, outline="#000000", tags="wx")
            city = getattr(self, "dest_city", "")
            if city:
                cv.create_text(dx, dy - r - 3, anchor="s", text=city, fill=AMBER, font=(FONT, int(9 * k), "bold"), tags="wx")
        r = 8 * k
        cv.create_oval(x - r, y - r, x + r, y + r, fill="#ffffff", outline="#000000", width=2, tags="wx")
        cv.create_oval(x - r / 2.5, y - r / 2.5, x + r / 2.5, y + r / 2.5, fill="#000000", outline="", tags="wx")

    def _header(self, w, h, f, k):
        cv = self.cv
        hour = f["hour"]
        when = f"{(hour % 12) or 12} {'AM' if hour < 12 else 'PM'}"
        import datetime as dt
        day = dt.date.fromisoformat(f["date"]).strftime("%a %b %d").replace(" 0", " ")
        ahead = "NOW" if self.i == 0 else f"+{self.i} h"
        cv.create_rectangle(0, 0, w, 30, fill="#15171b", outline="", tags="wx")
        cv.create_text(12, 15, anchor="w", text=f"WEATHER  ·  {day}  {when}", fill=TXT, font=(FONT, 11, "bold"), tags="wx")
        cv.create_text(w - 12, 15, anchor="e", text=("▶ " if self.playing else "❚❚ ") + ahead,
                       fill=AMBER if self.i == 0 else TXT, font=(FONT, 11, "bold"), tags="wx")
        # hour ticks along the bottom, the shown hour lit
        n = len(self.data["frames"])
        x0, x1, yb = 12, w * 0.55, h - 14
        for j in range(n):
            x = x0 + (x1 - x0) * j / max(1, n - 1)
            cv.create_line(x, yb - (7 if j == self.i else 3), x, yb + 3, fill=AMBER if j == self.i else DIM,
                           width=3 if j == self.i else 1, tags="wx")
        # legend
        items = [("Rain", RAIN[3][1]), ("Heavy", RAIN[1][1]), ("Storms", RAIN[0][1]), ("Snow", SNOW[1][1]), ("Fog", FOG)]
        x = w - 12
        for label, c in reversed(items):
            tw = len(label) * 7 + 6
            cv.create_text(x, yb, anchor="e", text=label, fill=DIM, font=(FONT, 8), tags="wx")
            x -= tw
            cv.create_rectangle(x - 10, yb - 5, x, yb + 5, fill=c, outline="", tags="wx")
            x -= 18
