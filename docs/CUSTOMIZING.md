# Customizing TruckBar

Most of TruckBar can be set up from its settings window, while you play. This guide covers that
window, then the parts that need a text editor:

1. [The settings window](#1-the-settings-window): no files to edit
2. [Hotkeys and macro pads](#2-hotkeys-and-macro-pads)
3. [The settings file](#3-the-settings-file): every value in `settings.json`
4. [Starting it with the game](#4-starting-it-with-the-game)
5. [Colours, tach bands and other built-in values](#5-colours-tach-bands-and-other-built-in-values): editing the code
6. [Feeding it your own weather](#6-feeding-it-your-own-weather): the forecast and map file formats

---

## 1. The settings window

Press **Ctrl + Alt + B**, even while driving. Or right-click the bar when the mouse is free, for
example in the game's menus. The window takes the mouse and keyboard while it's open.
**Done – back to the game** hands them straight back.

> ATS and ETS2 treat losing focus like letting go of the controls, so an H-shifter drops to
> neutral. Open the settings window when you're stopped.

| Section | What you can change |
|---|---|
| **Show on the bar** | Tick or untick each section. The bar shrinks to fit what's left. |
| **Look** | Opacity (20–100%) and size (50–200%). Gear names as on the dash (`5L`, `5H`) or plain numbers. Fuel-stop margin. Number of wiper speeds (2 or 3). 12- or 24-hour clock. |
| **Position** | Arrow buttons nudge the bar. **Centre along top** puts it back. **Lock position** stops accidental dragging. |
| **Weather map** | Which screen it opens on (main screen first, the others named by where they sit), which corner of it, its size as a percentage of that screen's width, and arrows to fine-tune. **Open / close the map** previews it. |
| **Keys** | The two hotkeys. See the next section. |

Every change is saved as you make it. The bar and the map move as you change them.

## 2. Hotkeys and macro pads

The game keeps the mouse while you drive, so two hotkeys work anywhere, even with the game in
front:

| Default | Opens / closes |
|---|---|
| **Ctrl + Alt + B** | the settings window |
| **Ctrl + Alt + W** | the weather map |

**To change one:** open settings, click the key under **Keys**, then press the new combination.
Esc cancels.

- A hotkey works in every program, so it needs **Ctrl, Alt, Shift or Win** plus a key. A plain
  letter would stop that letter typing everywhere, so TruckBar refuses it and says why.
- **F13–F24, Pause and Scroll Lock** are allowed on their own. No keyboard types with them, and
  many macro pads and gaming keyboards can send them. Where your pad's software offers them,
  they're the cleanest choice.
- **Combinations other programs already use:** if another program has registered the
  combination (Discord, GeForce Experience, AMD Software and others take a few), Windows refuses
  it. TruckBar tells you and keeps the old key. The window also flags any saved key that was
  taken when TruckBar started.

**On a macro pad or gaming keyboard** (Razer Synapse, Logitech G HUB, Corsair iCUE and
similar): assign a pad key to send the hotkey's combination, for example `Ctrl + Alt + W`. Put it
in the profile you use for the game. TruckBar never takes keyboard focus, so per-game profiles
stay active while it's running.

## 3. The settings file

Everything is kept in

```
%APPDATA%\TruckBar\settings.json
```

Paste that into Explorer's address bar to find it. Delete the file to go back to the defaults.

**Quit TruckBar before editing it.** TruckBar writes the file every time a setting changes, so
edits made while it's running are overwritten.

| Key | Default | What it is |
|---|---|---|
| `x` | `null` | Bar's left edge in screen pixels. `null` = centred on the main screen. |
| `y` | `0` | Bar's top edge in screen pixels. |
| `scale` | `1.0` | Size, `0.5` – `2.0`. |
| `alpha` | `0.85` | Opacity, `0.2` – `1.0`. |
| `locked` | `false` | `true` stops the bar being dragged. |
| `gear_names` | `"auto"` | `"auto"` names gears like your dash; `"numbers"` always shows 1, 2, 3… |
| `clock_24h` | `false` | `true` for a 24-hour clock. |
| `fuel_margin_km` | `120` | Range to keep in reserve when working out the fuel stop, **in km** even in ATS (80 km ≈ 50 mi). |
| `wiper_speeds` | `3` | `3` = intermittent, low, high. `2` = low, high. |
| `show` | all `true` | One `true`/`false` per section: `signals`, `speed`, `lamps`, `warnings`, `compass`, `fuel`, `gauges`, `job`, `weather`, `clock`. |
| `hotkeys` | see above | `{"settings": "Ctrl+Alt+B", "map": "Ctrl+Alt+W"}`. Modifiers `Ctrl`, `Alt`, `Shift`, `Win`, joined with `+`. Keys `A`–`Z`, `0`–`9`, `F1`–`F24`, `Num0`–`Num9`, `Space`, `Home`, `End`, `PageUp`, `PageDown`, `Insert`, `Delete`, `Up`/`Down`/`Left`/`Right`, `Pause`, `ScrollLock`, and the punctuation keys as themselves (`[`, `]`, `;`, `/` …). |
| `map_place` | main screen, top left | `[screen, corner]` as chosen in the settings window. The corner is one of `Top left`, `Top right`, `Centre`, `Bottom left`, `Bottom right`. |
| `map_geom` | — | `[x, y, width]` of the weather map in screen pixels. The height follows from the width. |

To keep a separate settings file, for example one per game or one for testing, set the
`TRUCKBAR_SETTINGS` environment variable to its path before starting TruckBar:

```
set TRUCKBAR_SETTINGS=C:\Tools\TruckBar\settings-ets2.json
pythonw truckbar.pyw
```

## 4. Starting it with the game

```
pythonw truckbar.pyw --exit-with-game
```

TruckBar waits for ATS or ETS2 to start, then closes itself once the game has quit. To make a
shortcut that starts both:

1. Right-click the desktop → **New → Shortcut**.
2. Location: `pythonw "C:\Tools\TruckBar\truckbar.pyw" --exit-with-game`, using wherever you
   unzipped TruckBar.
3. Start the game from Steam as usual, and use the shortcut before or after it.

Any launcher or script that starts your companion apps can run the same command.

## 5. Colours, tach bands and other built-in values

These live at the top of the code. Open the file in any text editor (Notepad works), change the
value, save, and restart TruckBar. Keep the quotes and commas as they are. If TruckBar won't
start after an edit, run `python truckbar.pyw --demo` from a command prompt. It prints the line
with the mistake.

**`truckbar.pyw`, near the top:**

| Name | Default | What it changes |
|---|---|---|
| `BG`, `PANEL`, `EDGE` | dark greys | Bar background, panel and border colours |
| `TXT`, `DIM`, `OFF` | | Main text, labels, unlit lamps |
| `GREEN`, `AMBER`, `RED`, `BLUE` | | Lit lamps, gauge zones, warnings, high beams |
| `WIPER_PURPLES` | three purples | Wiper speed colours, intermittent → high |
| `FONT` | `"Segoe UI"` | Font for everything on the bar |
| `RPM_GREEN` | `(0.52, 0.71)` | Tach economy band, as fractions of the engine's governed RPM |
| `RPM_AMBER`, `RPM_RED` | `0.81`, `0.905` | Where the tach turns amber and red |
| `FPS` | `12` | Redraws per second. Lower it if your CPU is tight; ATS is CPU-bound. |

Colours are web-style hex: `"#4cd964"`. Any colour picker gives you one.

**`mapwin.py`, near the top:**

| Name | What it changes |
|---|---|
| `OCEAN`, `LAND`, `BORDER`, `COAST` | Map background colours, as `(red, green, blue)` or `(red, green, blue, alpha)` from 0–255 |
| `RAIN_STOPS`, `SNOW_STOPS` | The radar colour scale: `(intensity, colour)` pairs, blended smoothly between |
| `FOG_RGB` | Fog shading |
| `COLD`, `WARM` | Cold- and warm-front colours |
| `ASPECT` | Map width ÷ height |
| `WEST`, `EAST`, `SOUTH`, `NORTH` | The area shown, in degrees. The default is the lower 48. |

Updating TruckBar replaces these files, so note your changes somewhere first.

## 6. Feeding it your own weather

The game's telemetry has no weather in it, so the weather pane and the weather map show data
written by a separate program. TruckBar only reads it. The pane stays hidden, and the map says it has no
weather yet, until its file exists and was written within the last 6 hours. Both are
plain JSON. Write to a temporary file and rename it over the old one, so TruckBar never reads
a half-written file.

### The forecast: weather pane and truck marker

`%APPDATA%\WeatherDirector\forecast.json` (or the path in `TRUCKBAR_FORECAST`), re-read every
5 seconds.

```json
{
  "updated": 1793520000,
  "date_label": "Nov 4",
  "place": "Texarkana, AR",
  "lat": 33.36, "lon": -94.09,
  "now": {"temp": 46, "sky": "Mostly cloudy"},
  "high": 52, "low": 38,
  "alert": "",
  "dest": [39.74, -104.99],
  "road": {
    "temp": 46, "now_sky": "Mostly cloudy", "high": 52, "low": 38,
    "ahead": "Rain in 1½ hours", "later": "At Denver: Rain, 42°",
    "wet_now": false
  }
}
```

| Field | Needed | Use |
|---|---|---|
| `updated` | yes | Unix time written. Older than 6 hours = ignored. |
| `date_label`, `place` | yes | Pane header. Only the part of `place` before the comma is shown. |
| `now.temp`, `now.sky` | yes | Shown when `road` doesn't give them |
| `high`, `low` | | Header, when there's no alert |
| `alert` | | Replaces high/low in amber, e.g. `"Winter storm"` |
| `road.*` | | What's ahead on your route. `ahead` and `later` are the pane's two lines. A line starting `Dry for` is drawn dimmed. `wet_now: true` draws `ahead` in blue. |
| `lat`, `lon` | for the map | Where the truck marker goes |
| `dest` | for the map | `[lat, lon]` of the destination. Draws the dashed line and square marker. |

Temperatures are in whatever unit you write; TruckBar doesn't convert them.

### The map: `wxmap.json`

`%APPDATA%\WeatherDirector\wxmap.json` (or the path in `TRUCKBAR_WXMAP`), checked every 3
seconds. One frame per hour; the map loops through them like a TV radar.

```json
{
  "updated": 1793520000,
  "lats": [50, 49, 48, "…", 24],
  "lons": [-125, -124, "…", -66],
  "cities": [{"name": "Seattle", "lat": 47.45, "lon": -122.31}],
  "frames": [{
    "date": "2026-11-04", "hour": 2,
    "rain": [0, 3, 12, "…"], "snow": ["…"], "fog": ["…"],
    "highs":  [{"kind": "H", "lat": 37, "lon": -70, "mb": 1035}],
    "lows":   [{"kind": "L", "lat": 33, "lon": -117, "mb": 992}],
    "fronts": [{"type": "cold", "pts": [[-125, 41], [-125.6, 38.5]]}],
    "events": [{"label": "Dense fog", "lat": 36.2, "lon": -120.4}],
    "temps":  [49]
  }]
}
```

- **The grid:** `lats` runs north to south and `lons` west to east. `rain`, `snow` and `fog`
  each list one value per grid point, row by row (all the longitudes for the first latitude,
  then the next). `len(lats) × len(lons)` values each.
- **Intensities are ×10:** `rain` and `snow` from 0 (none) to about 16 (`10` = heavy rain, `13`+
  = storms); `fog` is `0` or `1`. TruckBar smooths the grid, so 1° spacing is plenty.
- **`fronts`:** `type` is `cold`, `warm` or `stationary`, and `pts` are `[lon, lat]` points along
  the front. They're drawn as a smooth curve; cold-front triangles face east, warm-front
  half-circles face north.
- **`temps`:** one temperature per entry in `cities`, in the same order.
- **`date`, `hour`:** the game's calendar date and hour (0–23), shown in the map's header.
- **`events`:** labels drawn in amber at their position, with a ⚠.

The map also needs **Pillow** and **numpy** (`pip install pillow numpy`). The bar itself
doesn't.
