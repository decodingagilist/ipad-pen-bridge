"""Phase 2: stream the pen's target area back to the iPad so you can see what you write on."""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable, Optional

from monitors import Rect

log = logging.getLogger("penbridge.mirror")

try:
    import mss
    from PIL import Image
    AVAILABLE = True
except ImportError:  # mirroring is optional; pen input works without it
    AVAILABLE = False

# mss handles are not thread-safe, so all captures run on one dedicated thread.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
_local = threading.local()


def _grab_jpeg(rect: Rect, max_width: int, quality: int, last_crc: int) -> tuple[Optional[bytes], int]:
    if not hasattr(_local, "sct"):
        _local.sct = mss.mss()
    shot = _local.sct.grab({"left": rect.left, "top": rect.top,
                            "width": rect.width, "height": rect.height})
    crc = zlib.crc32(shot.bgra)
    if crc == last_crc:
        return None, crc  # nothing changed on screen, save the Wi-Fi
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), crc


async def stream(get_rect: Callable[[], Rect], send: Callable[[bytes], Awaitable[None]],
                 fps: int = 20, max_width: int = 1600, quality: int = 60) -> None:
    """Runs until cancelled. `send` awaits the socket, which gives natural backpressure."""
    if not AVAILABLE:
        log.warning("Mirroring needs: pip install mss Pillow")
        return
    loop = asyncio.get_running_loop()
    interval = 1 / fps
    last_crc, last_rect = 0, None
    while True:
        started = time.perf_counter()
        rect = get_rect()
        if rect != last_rect:
            last_crc, last_rect = 0, rect  # force a frame after switching monitor/region
        try:
            jpeg, last_crc = await loop.run_in_executor(
                _executor, _grab_jpeg, rect, max_width, quality, last_crc)
        except Exception as exc:  # e.g. secure desktop / UAC prompt blocks capture
            log.debug("capture failed: %s", exc)
            jpeg = None
        if jpeg:
            await send(jpeg)
        await asyncio.sleep(max(0.0, interval - (time.perf_counter() - started)))
