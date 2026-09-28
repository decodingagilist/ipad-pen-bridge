"""Turns iPad pen samples into real Windows pen input.

Uses the Synthetic Pointer API (Windows 10 1809+), so apps see a genuine pen
with pressure and tilt via Windows Ink - no driver install, no admin rights.
The hover/contact state machine is kept separate from the Win32 calls so it
can be unit-tested on any OS.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass

log = logging.getLogger("penbridge.injector")

# POINTER_FLAGS
PF_INRANGE, PF_INCONTACT, PF_FIRSTBUTTON = 0x2, 0x4, 0x10
PF_PRIMARY = 0x2000
PF_DOWN, PF_UPDATE, PF_UP = 0x10000, 0x20000, 0x40000
# PEN_FLAGS / PEN_MASK
PEN_FLAG_BARREL, PEN_FLAG_INVERTED, PEN_FLAG_ERASER = 0x1, 0x2, 0x4
PEN_MASK_PRESSURE, PEN_MASK_TILT_X, PEN_MASK_TILT_Y = 0x1, 0x4, 0x8
# POINTER_BUTTON_CHANGE_TYPE
BTN_NONE, BTN_FIRST_DOWN, BTN_FIRST_UP = 0, 1, 2

PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1

# Event codes sent by the iPad client
HOVER, DOWN, MOVE, UP, LEAVE = 0, 1, 2, 3, 4


@dataclass
class PenFrame:
    flags: int
    pen_flags: int
    x: int
    y: int
    pressure: int  # 0..1024
    tilt_x: int
    tilt_y: int
    button_change: int


class PenStateMachine:
    """Converts a stream of (event, x, y, pressure, tilt) into valid pointer frames.

    Windows rejects out-of-order frames (e.g. DOWN without being in range, UPDATE
    while in contact without a DOWN), so this fills in the missing transitions.
    """

    def __init__(self) -> None:
        self.in_range = False
        self.in_contact = False
        self.eraser = False
        self.last = (0, 0)

    def _pen_flags(self, contact: bool) -> int:
        if not self.eraser:
            return 0
        return PEN_FLAG_INVERTED | (PEN_FLAG_ERASER if contact else 0)

    def _frame(self, flags: int, x: int, y: int, p: float, tx: float, ty: float,
               contact: bool, button: int = BTN_NONE) -> PenFrame:
        pressure = max(1, min(1024, round(p * 1024))) if contact else 0
        return PenFrame(flags | PF_PRIMARY, self._pen_flags(contact), x, y,
                        pressure, _clamp_tilt(tx), _clamp_tilt(ty), button)

    def feed(self, event: int, x: int, y: int, p: float = 0.0,
             tx: float = 0.0, ty: float = 0.0) -> list[PenFrame]:
        out: list[PenFrame] = []
        if event == LEAVE:
            out += self.release(x, y)
            if self.in_range:
                out.append(self._frame(PF_UPDATE, x, y, 0, tx, ty, False))
                self.in_range = False
            return out

        if not self.in_range:
            out.append(self._frame(PF_INRANGE | PF_UPDATE, x, y, 0, tx, ty, False))
            self.in_range = True

        if event == HOVER or (event == MOVE and not self.in_contact):
            if self.in_contact:  # pen lifted without an UP (lost event)
                out += self.release(x, y)
            out.append(self._frame(PF_INRANGE | PF_UPDATE, x, y, 0, tx, ty, False))
        elif event == DOWN:
            if self.in_contact:
                out += self.release(*self.last)
            out.append(self._frame(PF_INRANGE | PF_INCONTACT | PF_FIRSTBUTTON | PF_DOWN,
                                   x, y, p, tx, ty, True, BTN_FIRST_DOWN))
            self.in_contact = True
        elif event == MOVE:
            out.append(self._frame(PF_INRANGE | PF_INCONTACT | PF_FIRSTBUTTON | PF_UPDATE,
                                   x, y, p, tx, ty, True))
        elif event == UP:
            out += self.release(x, y)
        self.last = (x, y)
        return out

    def release(self, x: int, y: int) -> list[PenFrame]:
        """Lift the pen if it is down - also used on disconnect so no stroke gets stuck."""
        if not self.in_contact:
            return []
        self.in_contact = False
        return [self._frame(PF_INRANGE | PF_UP, x, y, 0, 0, 0, False, BTN_FIRST_UP)]


def _clamp_tilt(v: float) -> int:
    return max(-90, min(90, round(v or 0)))


# ---------------------------------------------------------------- Win32 backend

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32),
                ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class POINTER_INFO(ctypes.Structure):
    _fields_ = [("pointerType", ctypes.c_uint32), ("pointerId", ctypes.c_uint32),
                ("frameId", ctypes.c_uint32), ("pointerFlags", ctypes.c_uint32),
                ("sourceDevice", ctypes.c_void_p), ("hwndTarget", ctypes.c_void_p),
                ("ptPixelLocation", POINT), ("ptHimetricLocation", POINT),
                ("ptPixelLocationRaw", POINT), ("ptHimetricLocationRaw", POINT),
                ("dwTime", ctypes.c_uint32), ("historyCount", ctypes.c_uint32),
                ("InputData", ctypes.c_int32), ("dwKeyStates", ctypes.c_uint32),
                ("PerformanceCount", ctypes.c_uint64), ("ButtonChangeType", ctypes.c_int32)]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("penFlags", ctypes.c_uint32),
                ("penMask", ctypes.c_uint32), ("pressure", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("tiltX", ctypes.c_int32),
                ("tiltY", ctypes.c_int32)]


class POINTER_TOUCH_INFO(ctypes.Structure):
    # Only here so the union below has the same size as the Windows SDK's.
    _fields_ = [("pointerInfo", POINTER_INFO), ("touchFlags", ctypes.c_uint32),
                ("touchMask", ctypes.c_uint32), ("rcContact", RECT), ("rcContactRaw", RECT),
                ("orientation", ctypes.c_uint32), ("pressure", ctypes.c_uint32)]


class _TypeInfoUnion(ctypes.Union):
    _fields_ = [("touchInfo", POINTER_TOUCH_INFO), ("penInfo", POINTER_PEN_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _TypeInfoUnion)]


class Injector:
    """Public entry point used by the server."""

    def __init__(self) -> None:
        self.state = PenStateMachine()
        self.backend = _make_backend()

    @property
    def mode(self) -> str:
        return self.backend.name

    def set_eraser(self, on: bool) -> None:
        self.state.eraser = bool(on)

    def feed(self, event: int, x: int, y: int, p: float, tx: float, ty: float) -> None:
        for frame in self.state.feed(event, x, y, p, tx, ty):
            self.backend.send(frame)

    def lift(self) -> None:
        x, y = self.state.last
        for frame in self.state.feed(LEAVE, x, y):
            self.backend.send(frame)


class _SyntheticPenBackend:
    name = "windows-pen"

    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._create = user32.CreateSyntheticPointerDevice
        self._create.restype = ctypes.c_void_p
        self._create.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        self._inject = user32.InjectSyntheticPointerInput
        self._inject.restype = ctypes.c_int
        self._inject.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINTER_TYPE_INFO), ctypes.c_uint32]
        self.device = self._create(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
        if not self.device:
            raise OSError(ctypes.get_last_error(), "CreateSyntheticPointerDevice failed")
        self._info = POINTER_TYPE_INFO()
        self._errors = 0

    def send(self, f: PenFrame) -> None:
        info = self._info
        ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
        info.type = PT_PEN
        pen = info.penInfo
        pen.pointerInfo.pointerType = PT_PEN
        pen.pointerInfo.pointerFlags = f.flags
        pen.pointerInfo.ptPixelLocation.x = f.x
        pen.pointerInfo.ptPixelLocation.y = f.y
        pen.pointerInfo.ButtonChangeType = f.button_change
        pen.penFlags = f.pen_flags
        pen.penMask = PEN_MASK_PRESSURE | PEN_MASK_TILT_X | PEN_MASK_TILT_Y
        pen.pressure = f.pressure
        pen.tiltX = f.tilt_x
        pen.tiltY = f.tilt_y
        if not self._inject(self.device, ctypes.byref(info), 1):
            self._errors += 1
            if self._errors <= 5 or self._errors % 500 == 0:
                log.warning("InjectSyntheticPointerInput failed (error %s, flags 0x%x)",
                            ctypes.get_last_error(), f.flags)


class _MouseBackend:
    """Fallback for older Windows builds: no pressure, but strokes still draw."""
    name = "windows-mouse"
    LEFTDOWN, LEFTUP = 0x2, 0x4

    def __init__(self) -> None:
        self.user32 = ctypes.windll.user32

    def send(self, f: PenFrame) -> None:
        self.user32.SetCursorPos(f.x, f.y)
        if f.flags & PF_DOWN:
            self.user32.mouse_event(self.LEFTDOWN, 0, 0, 0, 0)
        elif f.flags & PF_UP:
            self.user32.mouse_event(self.LEFTUP, 0, 0, 0, 0)


class _LogBackend:
    """Non-Windows dry run, so the server and iPad page can be tested anywhere."""
    name = "dry-run"

    def __init__(self) -> None:
        self.frames: list[PenFrame] = []

    def send(self, f: PenFrame) -> None:
        self.frames.append(f)
        if f.flags & (PF_DOWN | PF_UP):
            log.info("%s at (%d, %d) pressure %d", "DOWN" if f.flags & PF_DOWN else "UP",
                     f.x, f.y, f.pressure)


def _make_backend():
    if sys.platform != "win32":
        return _LogBackend()
    try:
        return _SyntheticPenBackend()
    except (OSError, AttributeError) as exc:
        log.warning("Synthetic pen unavailable (%s); falling back to mouse input without pressure", exc)
        return _MouseBackend()
