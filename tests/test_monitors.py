from monitors import Monitor, Rect, Target

# Laptop panel at 150% (physical 1920x1200) with a 2560x1440 monitor to its right.
LAPTOP = Monitor(0, "DISPLAY1", Rect(0, 0, 1920, 1200), True)
EXTERNAL = Monitor(1, "DISPLAY2", Rect(1920, -240, 4480, 1200), False)


def make():
    return Target([LAPTOP, EXTERNAL])


def test_starts_on_primary_and_maps_corners():
    t = make()
    assert t.index == 0
    assert t.map(0, 0) == (0, 0)
    assert t.map(1, 1) == (1919, 1199)


def test_external_monitor_uses_its_own_offset():
    t = make()
    t.select(1)
    assert t.map(0, 0) == (1920, -240)
    assert t.map(0.5, 0.5) == (1920 + 1280, -240 + 720)
    assert abs(t.describe()["aspect"] - 2560 / 1440) < 1e-9


def test_out_of_range_input_is_clamped():
    t = make()
    assert t.map(-0.2, 1.7) == (0, 1199)


def test_cycle_wraps_and_clears_region():
    t = make()
    t.set_region_normalised(0.25, 0.25, 0.75, 0.75)
    assert t.region is not None
    t.cycle()
    assert t.index == 1 and t.region is None
    t.cycle()
    assert t.index == 0


def test_region_from_drag_in_any_direction():
    t = make()
    t.select(1)
    t.set_region_normalised(0.75, 1.0, 0.25, 0.5)  # dragged bottom-right to top-left
    assert t.rect == Rect(1920 + 640, -240 + 720, 1920 + 1920, 1200)
    assert t.map(0, 0) == (2560, 480)


def test_tiny_region_is_ignored():
    t = make()
    t.set_region_normalised(0.5, 0.5, 0.51, 0.51)
    assert t.region is None


def test_fit_to_window_picks_monitor_with_most_overlap():
    t = make()
    window = Rect(1800, 100, 3000, 900)  # mostly on the external monitor
    assert t.set_region_absolute(window)
    assert t.index == 1
    assert t.rect == Rect(1920, 100, 3000, 900)


def test_fit_to_window_rejects_offscreen():
    t = make()
    assert not t.set_region_absolute(Rect(-5000, -5000, -4000, -4000))
    assert t.region is None
