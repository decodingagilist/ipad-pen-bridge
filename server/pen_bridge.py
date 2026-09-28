"""PenBridge server: iPad + Apple Pencil -> Windows pen input.

Run on the Windows laptop:  python server/pen_bridge.py
Then open the printed URL in Safari on the iPad (same Wi-Fi or the laptop's hotspot).
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import secrets
import socket
import threading
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

import mirror
from injector import Injector
from monitors import IS_WINDOWS, Target, enumerate_monitors, foreground_window_rect, set_dpi_aware

log = logging.getLogger("penbridge")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TOKEN_FILE = Path.home() / ".penbridge_token"


class Bridge:
    def __init__(self, token: str) -> None:
        self.token = token
        self.target = Target(enumerate_monitors())
        self.injector = Injector()
        self.client: Optional[web.WebSocketResponse] = None
        self.mirror_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- helpers
    async def push_config(self) -> None:
        if self.client is not None and not self.client.closed:
            await self.client.send_json({"t": "config", **self.target.describe(),
                                         "mode": self.injector.mode,
                                         "mirrorAvailable": mirror.AVAILABLE})

    def retarget(self) -> None:
        """Called after any monitor/region change: lift the pen and tell the iPad."""
        self.injector.lift()
        asyncio.ensure_future(self.push_config())
        log.info("Pen target: %s %s", self.target.monitor.describe()["label"], self.target.rect)

    def stop_mirror(self) -> None:
        if self.mirror_task:
            self.mirror_task.cancel()
            self.mirror_task = None

    def start_mirror(self, ws: web.WebSocketResponse) -> None:
        self.stop_mirror()
        self.mirror_task = asyncio.ensure_future(
            mirror.stream(lambda: self.target.rect, ws.send_bytes))

    # ---------------------------------------------------------------- handlers
    async def index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1 << 20)
        await ws.prepare(request)

        try:
            hello = await ws.receive_json(timeout=10)
        except Exception:
            await ws.close(code=4001, message=b"bad hello")
            return ws
        if not secrets.compare_digest(str(hello.get("token", "")), self.token):
            log.warning("Rejected connection from %s (wrong token)", request.remote)
            await ws.close(code=4003, message=b"wrong token")
            return ws

        if self.client is not None and not self.client.closed:
            await self.client.close(code=4000, message=b"replaced by another device")
        self.stop_mirror()
        self.client = ws
        log.info("iPad connected from %s", request.remote)
        await self.push_config()

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        self.handle(json.loads(msg.data), ws)
                    except (ValueError, KeyError, TypeError) as exc:
                        log.debug("Ignored bad message: %s", exc)
        finally:
            self.injector.lift()
            if self.client is ws:
                self.stop_mirror()
                self.client = None
            log.info("iPad disconnected")
        return ws

    def handle(self, m: dict, ws: web.WebSocketResponse) -> None:
        t = m.get("t")
        if t == "b":  # batch of pen samples: [event, x, y, pressure, tiltX, tiltY]
            for ev, nx, ny, p, tx, ty in m["pts"]:
                x, y = self.target.map(nx, ny)
                self.injector.feed(int(ev), x, y, float(p), float(tx), float(ty))
        elif t == "monitor":
            self.target.select(int(m["index"]))
            self.retarget()
        elif t == "region":
            rect = m.get("rect")
            if rect:
                self.target.set_region_normalised(*rect)
            else:
                self.target.region = None
            self.retarget()
        elif t == "fitWindow":
            rect = foreground_window_rect()
            if not (rect and self.target.set_region_absolute(rect)):
                log.info("Fit to window: no usable foreground window")
            self.retarget()
        elif t == "eraser":
            self.injector.lift()
            self.injector.set_eraser(bool(m.get("on")))
        elif t == "mirror":
            if m.get("on"):
                self.start_mirror(ws)
            else:
                self.stop_mirror()
        elif t == "key":
            vks = [int(v) for v in m.get("vks", [])]
            if vks:
                self.injector.key_combo(*vks)
        elif t == "scroll":
            nx, ny = m.get("x", 0.5), m.get("y", 0.5)
            x, y = self.target.map(nx, ny)
            direction = int(m.get("direction", 0))
            if direction:
                self.injector.do_scroll(x, y, direction)


# -------------------------------------------------------------------- hotkeys

def start_hotkeys(loop: asyncio.AbstractEventLoop, bridge: Bridge) -> None:
    """Ctrl+Alt+Right / Ctrl+Alt+Left cycle the pen between monitors."""
    if not IS_WINDOWS:
        return
    import ctypes
    from ctypes import wintypes

    def run() -> None:
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
        keys = {1: (0x27, 1), 2: (0x25, -1)}  # VK_RIGHT, VK_LEFT
        for hk_id, (vk, _) in keys.items():
            if not user32.RegisterHotKey(None, hk_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
                log.warning("Hotkey %d already in use by another app", hk_id)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in keys:
                step = keys[msg.wParam][1]
                loop.call_soon_threadsafe(lambda s=step: (bridge.target.cycle(s), bridge.retarget()))

    threading.Thread(target=run, daemon=True, name="hotkeys").start()


# -------------------------------------------------------------------- startup

def load_token(rotate: bool) -> str:
    if not rotate and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(9)
    TOKEN_FILE.write_text(token)
    return token


def lan_addresses() -> list[str]:
    found: list[str] = []
    try:  # the interface used for the default route (no packet is actually sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:  # also picks up the Windows Mobile Hotspot adapter (usually 192.168.137.1)
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for a in found:
        ip = ipaddress.ip_address(a)
        if ip.is_private and not ip.is_loopback and a not in out:
            out.append(a)
    return out


def print_banner(port: int, token: str, show_qr: bool) -> None:
    urls = [f"http://{a}:{port}/#t={token}" for a in lan_addresses()] or \
           [f"http://<this-laptop-ip>:{port}/#t={token}"]
    print("\n  PenBridge is running. On the iPad, open in Safari:\n")
    for u in urls:
        print(f"    {u}")
    print("\n  Then Share -> Add to Home Screen for a full-screen app.")
    print("  Ctrl+Alt+Left/Right switches monitors. Ctrl+C to stop.\n")
    if show_qr:
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(urls[0])
            qr.print_ascii(invert=True)
        except ImportError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="iPad + Apple Pencil as a Windows pen tablet")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--new-token", action="store_true", help="rotate the pairing token")
    ap.add_argument("--no-qr", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    set_dpi_aware()
    bridge = Bridge(load_token(args.new_token))
    for m in bridge.target.monitors:
        log.info("Found %s: %s %dx%d", m.describe()["label"], m.name, m.rect.width, m.rect.height)
    log.info("Input mode: %s", bridge.injector.mode)

    app = web.Application()
    app.router.add_get("/", bridge.index)
    app.router.add_get("/ws", bridge.websocket)
    app.router.add_get("/favicon.ico", lambda _r: web.Response(status=204))
    app.router.add_static("/s/", WEB_DIR)

    async def on_startup(_app: web.Application) -> None:
        start_hotkeys(asyncio.get_running_loop(), bridge)
        print_banner(args.port, bridge.token, not args.no_qr)

    app.on_startup.append(on_startup)
    # 0.0.0.0 so both home Wi-Fi and the laptop hotspot work; the token gates access.
    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
