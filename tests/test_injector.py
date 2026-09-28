import ctypes
import struct

import pytest

from injector import (DOWN, HOVER, LEAVE, MOVE, PEN_FLAG_ERASER, PEN_FLAG_INVERTED, PF_DOWN,
                      PF_INCONTACT, PF_INRANGE, PF_UP, PF_UPDATE, POINTER_INFO, POINTER_PEN_INFO,
                      POINTER_TOUCH_INFO, POINTER_TYPE_INFO, UP, PenStateMachine)


def kinds(frames):
    out = []
    for f in frames:
        if f.flags & PF_DOWN:
            out.append("down")
        elif f.flags & PF_UP:
            out.append("up")
        elif f.flags & PF_INCONTACT:
            out.append("drag")
        elif f.flags & PF_INRANGE:
            out.append("hover")
        else:
            out.append("out")
    return out


def test_normal_stroke():
    sm = PenStateMachine()
    seq = []
    seq += sm.feed(HOVER, 10, 10)
    seq += sm.feed(DOWN, 10, 10, 0.5)
    seq += sm.feed(MOVE, 12, 12, 0.6)
    seq += sm.feed(UP, 12, 12)
    seq += sm.feed(LEAVE, 12, 12)
    assert kinds(seq) == ["hover", "hover", "down", "drag", "up", "out"]
    assert seq[2].pressure == 512 and seq[3].pressure == 614
    assert all(f.flags & PF_UPDATE for f in (seq[0], seq[1], seq[3]))


def test_down_without_hover_enters_range_first():
    # iPads without Pencil hover send pointerdown straight away.
    sm = PenStateMachine()
    assert kinds(sm.feed(DOWN, 5, 5, 0.0)) == ["hover", "down"]


def test_contact_pressure_never_zero():
    sm = PenStateMachine()
    frames = sm.feed(DOWN, 5, 5, 0.0)
    assert frames[-1].pressure >= 1


def test_lost_up_is_repaired():
    sm = PenStateMachine()
    sm.feed(DOWN, 1, 1, 0.5)
    assert kinds(sm.feed(DOWN, 9, 9, 0.5)) == ["up", "down"]
    assert kinds(sm.feed(HOVER, 9, 9)) == ["up", "hover"]


def test_leave_mid_stroke_lifts_pen():
    sm = PenStateMachine()
    sm.feed(DOWN, 1, 1, 0.5)
    assert kinds(sm.feed(LEAVE, 1, 1)) == ["up", "out"]
    assert not sm.in_contact and not sm.in_range


def test_eraser_flags():
    sm = PenStateMachine()
    sm.eraser = True
    hover, down = sm.feed(DOWN, 1, 1, 0.5)
    assert hover.pen_flags == PEN_FLAG_INVERTED
    assert down.pen_flags == PEN_FLAG_INVERTED | PEN_FLAG_ERASER


def test_tilt_clamped():
    sm = PenStateMachine()
    f = sm.feed(DOWN, 1, 1, 0.5, 120, -200)[-1]
    assert (f.tilt_x, f.tilt_y) == (90, -90)


@pytest.mark.skipif(struct.calcsize("P") != 8, reason="sizes checked for 64-bit Windows")
def test_struct_sizes_match_windows_sdk_x64():
    assert ctypes.sizeof(POINTER_INFO) == 96
    assert ctypes.sizeof(POINTER_PEN_INFO) == 120
    assert ctypes.sizeof(POINTER_TOUCH_INFO) == 144
    assert ctypes.sizeof(POINTER_TYPE_INFO) == 152
    assert POINTER_TYPE_INFO.penInfo.offset == 8
