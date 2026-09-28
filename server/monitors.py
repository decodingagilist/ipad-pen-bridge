"""Monitor discovery and mapping of normalised iPad coordinates to desktop pixels.

All rectangles are in physical pixels in the Windows virtual-desktop coordinate
space, which is why the process must be made per-monitor DPI aware first.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Optional

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0

    def intersect(self, other: "Rect") -> Optional["Rect"]:
        r = Rect(max(self.left, other.left), max(self.top, other.top),
                 min(self.right, other.right), min(self.bottom, other.bottom))
        return r if r.width > 0 and r.height > 0 else None


@dataclass(frozen=True)
class Monitor:
    index: int
    name: str
    rect: Rect
    primary: bool

    def describe(self) -> dict:
        label = "Main" if self.primary else f"Screen {self.index + 1}"
        return {"index": self.index, "label": label, "name": self.name,
                "width": self.rect.width, "height": self.rect.height,
                "primary": self.primary}


def set_dpi_aware() -> None:
    """Make coordinates physical pixels on mixed-DPI setups (e.g. laptop 150%, monitor 100%)."""
    if not IS_WINDOWS:
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def enumerate_monitors() -> list[Monitor]:
    """Return monitors ordered left-to-right, top-to-bottom as they sit on the desk."""
    if not IS_WINDOWS:
        return [Monitor(0, "virtual-1920x1080", Rect(0, 0, 1920, 1080), True)]

    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    user32 = ctypes.windll.user32
    found: list[tuple[Rect, bool, str]] = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _callback(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append((Rect(r.left, r.top, r.right, r.bottom),
                          bool(info.dwFlags & 1), info.szDevice.replace("\\\\.\\", "")))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    found.sort(key=lambda m: (m[0].left, m[0].top))
    return [Monitor(i, name, rect, primary) for i, (rect, primary, name) in enumerate(found)]


def foreground_window_rect() -> Optional[Rect]:
    """Bounds of the window the user is working in (e.g. OneNote), for 'Fit to window'."""
    if not IS_WINDOWS:
        return None
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    r = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS (9) excludes the invisible resize border GetWindowRect includes.
    try:
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0
    except (AttributeError, OSError):
        ok = False
    if not ok and not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return Rect(r.left, r.top, r.right, r.bottom)


@dataclass
class Target:
    """Which monitor (and optionally which part of it) the pen is mapped to."""
    monitors: list[Monitor]
    index: int = 0
    region: Optional[Rect] = None  # absolute desktop pixels, always inside the active monitor

    def __post_init__(self) -> None:
        if not self.monitors:
            raise ValueError("no monitors found")
        self.index = next((m.index for m in self.monitors if m.primary), 0)

    @property
    def monitor(self) -> Monitor:
        return self.monitors[self.index]

    @property
    def rect(self) -> Rect:
        return self.region or self.monitor.rect

    def select(self, index: int) -> None:
        self.index = index % len(self.monitors)
        self.region = None

    def cycle(self, step: int = 1) -> None:
        self.select(self.index + step)

    def set_region_normalised(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """Region given as fractions of the active monitor (as dragged on the mirror image)."""
        m = self.monitor.rect
        xs = sorted((_clamp01(x0), _clamp01(x1)))
        ys = sorted((_clamp01(y0), _clamp01(y1)))
        r = Rect(m.left + round(xs[0] * m.width), m.top + round(ys[0] * m.height),
                 m.left + round(xs[1] * m.width), m.top + round(ys[1] * m.height))
        self.region = r if r.width >= 50 and r.height >= 50 else None

    def set_region_absolute(self, rect: Rect) -> bool:
        """Lock to a desktop rectangle (e.g. a window); switches to the monitor it is mostly on."""
        best = max(self.monitors, key=lambda m: _area(m.rect.intersect(rect)))
        clipped = best.rect.intersect(rect)
        if clipped is None or clipped.width < 50 or clipped.height < 50:
            return False
        self.index = best.index
        self.region = clipped
        return True

    def map(self, nx: float, ny: float) -> tuple[int, int]:
        """Normalised (0..1) point in the iPad's active area -> desktop pixel."""
        r = self.rect
        return (r.left + round(_clamp01(nx) * (r.width - 1)),
                r.top + round(_clamp01(ny) * (r.height - 1)))

    def describe(self) -> dict:
        return {"monitors": [m.describe() for m in self.monitors], "active": self.index,
                "aspect": self.rect.aspect, "region": self.region is not None}


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else float(v)


def _area(r: Optional[Rect]) -> int:
    return r.width * r.height if r else 0
