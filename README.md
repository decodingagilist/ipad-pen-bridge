# PenBridge

Use an iPad and Apple Pencil as a pressure-sensitive pen tablet for a Windows laptop, across multiple monitors.

It's free. There's nothing to install on the iPad and no drivers on Windows.

```
iPad Safari page ──Wi-Fi / laptop hotspot──▶ Python server on Windows ──▶ real pen input (Windows Ink)
                 ◀────── screen mirror ─────
```

## What you need

**iPad:** Safari and the Apple Pencil. Nothing to install.

**Windows 10 (1809+) or 11 laptop:**
- Python 3.10+. Install it from python.org, or run `winget install Python.Python.3.12`.
- Both devices on the same Wi-Fi network, **or** the laptop's Mobile Hotspot turned on with the iPad joined to it. The hotspot works anywhere, with no router.

Bluetooth and the USB cable are not used for data. Keep the cable in if you want the iPad charging while you write.

## Set up (once)

```powershell
git clone <this repo> ; cd ipad-pen-bridge
py -m pip install -r requirements.txt
py server\pen_bridge.py
```

1. On the first run, Windows Firewall asks about Python. Tick **Private networks** and click Allow.
2. The terminal prints a URL such as `http://192.168.1.20:8765/#t=abc123` and a QR code. Scan the QR code with the iPad camera, or type the URL into Safari.
3. In Safari, tap **Share → Add to Home Screen**. From then on, PenBridge opens full-screen from its icon.

The pairing code is saved in `%USERPROFILE%\.penbridge_token` and stays the same between runs. Run with `--new-token` to change it.

## Daily use

1. Run `py server\pen_bridge.py` on the laptop and open PenBridge on the iPad.
2. Write with the Pencil. Finger touches are ignored, so you can rest your palm.
3. Tap **⚙︎** for these options:

| Option | What it does |
|---|---|
| **Pen goes to** | Choose the laptop screen or the external monitor. |
| **Whole screen** | Maps the full iPad area to the whole monitor. |
| **Fit to window** | Locks the pen to the window that's active on the laptop (for example OneNote). This gives much better precision on a big monitor. |
| **Draw area** | Drag a box on the mirrored screen to lock the pen to exactly that part. |
| **Show screen** | Mirrors the target area onto the iPad so you can see what you're writing on. |
| **Eraser** | Makes the Pencil act as an eraser in OneNote, Whiteboard and other Windows Ink apps. |
| **Clear ink** | Clears the preview ink on the iPad only. Nothing on the laptop changes. |

On the laptop, **Ctrl+Alt+→ / ←** also moves the pen to the next or previous monitor.

## Troubleshooting

| Symptom | Fix |
|---|---|
| iPad can't load the page | Check both devices are on the same network. Guest, hotel and corporate Wi-Fi often block device-to-device traffic, so use the laptop hotspot instead. Also re-check the firewall prompt: Windows Security → Firewall → Allow an app → Python → Private. |
| Status shows `mouse mode (no pressure)` | Your Windows build predates the synthetic pen API. Update Windows. |
| Ink lands in the wrong place on the external monitor | Restart the server after changing display scaling or plugging in a monitor. |
| Mirror is laggy | Use 5 GHz Wi-Fi or the hotspot. Pen input still works with **Show screen** turned off. |
| Strokes are not pressure-sensitive in an app | The app must support Windows Ink. OneNote, Whiteboard, PowerPoint, Krita and browsers do; Paint is basic. |

Only use this on a laptop you own or administer. Client-managed machines often block Python installs and inbound firewall rules.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest tests        # unit tests run on any OS
python server/pen_bridge.py   # on macOS/Linux it runs in dry-run mode and logs pen events
```

- `server/injector.py`: hover/contact state machine plus the Windows `InjectSyntheticPointerInput` backend.
- `server/monitors.py`: monitor discovery and mapping iPad coordinates to physical desktop pixels (DPI-aware).
- `server/mirror.py`: screen capture (mss) sent to the iPad as JPEG.
- `web/`: the iPad page, which uses Pointer Events with `pointerType === "pen"`, pressure, tilt and coalesced events.
