#!/usr/bin/env python3
"""Stream ONLY the Cursor IDE window to your phone browser — low-latency build.

Phone: open the printed URL (Tailscale IP preferred over Cloudflare tunnel).
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from ctypes import wintypes
from typing import Optional, Tuple

import cv2
import mss
import numpy as np
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import file_browser
from agent_bridge import AGENT, list_cdp_targets

try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pywin32 required") from exc


def enable_dpi_awareness() -> None:
    """Match screen coords with mss / SetCursorPos under Windows display scaling."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


enable_dpi_awareness()


def window_screen_rect(hwnd: int) -> Tuple[int, int, int, int]:
    """Visible window bounds (left, top, right, bottom) in screen pixels."""
    try:
        rect = wintypes.RECT()
        # DWMWA_EXTENDED_FRAME_BOUNDS = 9 — excludes invisible Win10+ resize margins
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)),
            9,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        pass
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return int(left), int(top), int(right), int(bottom)

PORT = int(os.environ.get("CURSORDESK_PORT", "8765"))
# Prefer readable text on phone over max FPS (tunnel bandwidth still matters)
JPEG_QUALITY = int(os.environ.get("CURSORDESK_JPEG_QUALITY", "74"))
TARGET_FPS = float(os.environ.get("CURSORDESK_FPS", "18"))
MAX_WIDTH = int(os.environ.get("CURSORDESK_MAX_WIDTH", "900"))
# Phone-shaped Cursor window (narrow + tall) so chat shows multiple messages
STREAM_W = int(os.environ.get("CURSORDESK_WIN_W", "780"))
STREAM_H = int(os.environ.get("CURSORDESK_WIN_H", "1560"))
PROJECT = os.environ.get(
    "CURSORDESK_PROJECT",
    os.path.expanduser("~/Documents"),
)

# Runtime knobs (phone can change via WS)
_settings_lock = threading.Lock()
_settings = {
    "quality": JPEG_QUALITY,
    "fps": TARGET_FPS,
    "max_width": MAX_WIDTH,
    "preset": "sharp",
}

PRESETS = {
    "smooth": {"quality": 42, "fps": 28, "max_width": 720},
    "balanced": {"quality": 58, "fps": 22, "max_width": 820},
    "sharp": {"quality": 74, "fps": 18, "max_width": 900},
    "ultra": {"quality": 86, "fps": 12, "max_width": 980},
}


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips and not ip.startswith("127."):
            ips.insert(0, ip)
    except Exception:
        pass
    return ips


def tailscale_ips() -> list[str]:
    exe = shutil.which("tailscale")
    if not exe:
        candidate = r"C:\Program Files\Tailscale\tailscale.exe"
        exe = candidate if os.path.isfile(candidate) else None
    if not exe:
        return []
    try:
        out = subprocess.check_output(
            [exe, "ip", "-4"], stderr=subprocess.DEVNULL, text=True, timeout=5
        )
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def find_cursor_hwnd() -> int:
    found = 0
    best_area = 0

    def enum_handler(hwnd, _):
        nonlocal found, best_area
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        low = title.lower()
        if "cursordesk" in low or "cursor stream" in low:
            return
        if "cursor" not in low:
            return
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            area = max(0, right - left) * max(0, bottom - top)
        except Exception:
            return
        if area < 200 * 200:
            return
        if area >= best_area:
            best_area = area
            found = hwnd

    win32gui.EnumWindows(enum_handler, None)
    return found


def cdp_available() -> bool:
    return bool(list_cdp_targets())


def cursor_exe() -> str:
    return os.path.expandvars(r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe")


def launch_cursor_with_cdp() -> None:
    exe = cursor_exe()
    if not os.path.isfile(exe):
        return
    args = [exe, "--remote-debugging-port=9222"]
    if PROJECT and os.path.isdir(PROJECT):
        args.append(PROJECT)
    subprocess.Popen(args, close_fds=True)


def focus_cursor(resize_for_phone: bool = False) -> int:
    """Find Cursor window. Never steals focus/size unless resize_for_phone=True.

    Auto-resize was breaking the real Cursor UI (Agents bottom bar etc.) and
    kept re-applying after the CDP helper window was closed — that was the
    capture host loop, not CDP itself.
    """
    hwnd = find_cursor_hwnd()
    if not hwnd:
        launch_cursor_with_cdp()
        for _ in range(40):
            time.sleep(0.25)
            hwnd = find_cursor_hwnd()
            if hwnd:
                break
    if hwnd and resize_for_phone:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                vs_left = win32api.GetSystemMetrics(76)
                vs_top = win32api.GetSystemMetrics(77)
            except Exception:
                vs_left, vs_top = 0, 0
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOP,
                vs_left + 40,
                vs_top + 40,
                STREAM_W,
                STREAM_H,
                0,
            )
        except Exception:
            pass
    return hwnd


def capture_region(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Screen region actually captured (left, top, width, height)."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    try:
        left, top, right, bottom = window_screen_rect(hwnd)
    except Exception:
        return None
    width = max(0, right - left)
    height = max(0, bottom - top)
    if width < 64 or height < 64:
        return None

    # Clamp to virtual screen (same space used for click mapping)
    vs_left = win32api.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vs_top = win32api.GetSystemMetrics(77)
    vs_w = win32api.GetSystemMetrics(78)
    vs_h = win32api.GetSystemMetrics(79)
    left = max(vs_left, left)
    top = max(vs_top, top)
    right = min(vs_left + vs_w, right)
    bottom = min(vs_top + vs_h, bottom)
    width = right - left
    height = bottom - top
    if width < 64 or height < 64:
        return None
    return left, top, width, height


def capture_bgr(
    hwnd: int, region: Optional[Tuple[int, int, int, int]] = None
) -> Optional[np.ndarray]:
    """Fast path: grab screen region of the Cursor window (best for Electron)."""
    region = region or capture_region(hwnd)
    if not region:
        return None
    left, top, width, height = region
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        # BGRA -> BGR
        frame = np.frombuffer(shot.raw, dtype=np.uint8).reshape(height, width, 4)
        return frame[:, :, :3].copy()


def encode_jpeg(frame: np.ndarray, quality: int, max_width: int) -> bytes:
    h, w = frame.shape[:2]
    if w > max_width:
        nh = max(1, int(h * (max_width / float(w))))
        frame = cv2.resize(frame, (max_width, nh), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), int(quality), int(cv2.IMWRITE_JPEG_OPTIMIZE), 0],
    )
    if not ok:
        return b""
    return buf.tobytes()


def client_to_screen(hwnd: int, nx: float, ny: float) -> Tuple[int, int]:
    # Prefer the exact region last shown on the phone so taps match pixels
    region = HUB.cap_rect
    if not region:
        region = capture_region(hwnd)
    if not region:
        left, top, right, bottom = window_screen_rect(hwnd)
        region = (left, top, max(1, right - left), max(1, bottom - top))
    left, top, w, h = region
    w = max(1, int(w))
    h = max(1, int(h))
    x = int(left + max(0.0, min(1.0, nx)) * (w - 1))
    y = int(top + max(0.0, min(1.0, ny)) * (h - 1))
    return x, y


def mouse_down(x: int, y: int, button: str = "left") -> None:
    try:
        hwnd = HUB.hwnd or find_cursor_hwnd()
        if hwnd:
            win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    win32api.SetCursorPos((x, y))
    flag = (
        win32con.MOUSEEVENTF_RIGHTDOWN
        if button == "right"
        else win32con.MOUSEEVENTF_LEFTDOWN
    )
    win32api.mouse_event(flag, 0, 0, 0, 0)


def mouse_up(x: int, y: int, button: str = "left") -> None:
    win32api.SetCursorPos((x, y))
    flag = (
        win32con.MOUSEEVENTF_RIGHTUP
        if button == "right"
        else win32con.MOUSEEVENTF_LEFTUP
    )
    win32api.mouse_event(flag, 0, 0, 0, 0)


def mouse_move(x: int, y: int) -> None:
    win32api.SetCursorPos((x, y))


def mouse_wheel(delta: int, x: int | None = None, y: int | None = None) -> None:
    # Windows expects multiples of WHEEL_DELTA (120)
    if x is not None and y is not None:
        try:
            win32api.SetCursorPos((int(x), int(y)))
        except Exception:
            pass
    steps = int(delta)
    if steps == 0:
        return
    # Clamp and quantize to 120
    if abs(steps) < 120:
        steps = 120 if steps > 0 else -120
    else:
        steps = int(round(steps / 120.0)) * 120
    steps = max(-720, min(720, steps))
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, steps, 0)


def type_text(text: str) -> None:
    for ch in text:
        if ch == "\n":
            win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
            win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
            continue
        if ch == "\b":
            win32api.keybd_event(win32con.VK_BACK, 0, 0, 0)
            win32api.keybd_event(win32con.VK_BACK, 0, win32con.KEYEVENTF_KEYUP, 0)
            continue
        try:
            vk = win32api.VkKeyScan(ch)
            if vk == -1:
                continue
            lo = vk & 0xFF
            shift = bool(vk & 0x100)
            if shift:
                win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
            win32api.keybd_event(lo, 0, 0, 0)
            win32api.keybd_event(lo, 0, win32con.KEYEVENTF_KEYUP, 0)
            if shift:
                win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception:
            continue


class FrameHub:
    def __init__(self) -> None:
        self.jpeg: bytes = b""
        self.lock = threading.Lock()
        self.hwnd = 0
        self.cap_rect: Optional[Tuple[int, int, int, int]] = None
        self.viewers = 0
        self.fps_real = 0.0
        self.encode_ms = 0.0
        self._stop = threading.Event()
        self._seq = 0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="cursor-capture", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        frames = 0
        t_fps = time.time()
        while not self._stop.is_set():
            with _settings_lock:
                quality = int(_settings["quality"])
                fps = float(_settings["fps"])
                max_width = int(_settings["max_width"])
            interval = 1.0 / max(1.0, fps)

            # Don't burn CPU with zero viewers
            if self.viewers <= 0:
                time.sleep(0.25)
                continue

            t0 = time.time()
            # Do NOT call focus_cursor() here — resizing/foreground fights the
            # real Cursor UI. Just capture whatever window exists.
            hwnd = find_cursor_hwnd()
            self.hwnd = hwnd
            region = capture_region(hwnd) if hwnd else None
            frame = capture_bgr(hwnd, region) if region else None
            if frame is not None:
                jpeg = encode_jpeg(frame, quality=quality, max_width=max_width)
                if jpeg:
                    with self.lock:
                        self.jpeg = jpeg
                        self.cap_rect = region
                        self._seq += 1
                self.encode_ms = (time.time() - t0) * 1000.0
                # Mild adaptive quality if encode is too slow (keep text readable)
                if self.encode_ms > (interval * 1000.0 * 0.95) and quality > 58:
                    with _settings_lock:
                        _settings["quality"] = max(58, int(_settings["quality"]) - 2)

            frames += 1
            if time.time() - t_fps >= 1.0:
                self.fps_real = frames / (time.time() - t_fps)
                frames = 0
                t_fps = time.time()

            dt = time.time() - t0
            time.sleep(max(0.0, interval - dt))


HUB = FrameHub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not getattr(HUB, "_thread_started", False):
        hwnd = find_cursor_hwnd()
        print(f"Cursor hwnd={hwnd or 0} (not resizing - leave your layout alone)")
        if cdp_available():
            print("Cursor CDP :9222 OK")
        else:
            print(
                "WARNING: Cursor CDP not on :9222 - Agent tab needs "
                "Host\\Start-CursorDesk.bat -Relaunch (fully quit Cursor first)."
            )
        HUB.start()
        HUB._thread_started = True
    AGENT.start()
    try:
        yield
    finally:
        try:
            await AGENT.stop()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _read_web(name: str) -> str:
    path = os.path.join(WEB_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(
        _read_web("index.html"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/app.js")
def app_js() -> HTMLResponse:
    return HTMLResponse(
        _read_web("app.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/app.css")
def app_css() -> HTMLResponse:
    return HTMLResponse(
        _read_web("app.css"),
        media_type="text/css",
        headers={"Cache-Control": "no-store, max-age=0"},
    )



@app.get("/health")
def health() -> dict:
    with _settings_lock:
        settings = dict(_settings)
    agent = AGENT.state
    return {
        "ok": True,
        "hwnd": int(HUB.hwnd or 0),
        "hasFrame": bool(HUB.jpeg),
        "fps": HUB.fps_real,
        "encode_ms": HUB.encode_ms,
        "settings": settings,
        "ips": lan_ips(),
        "tailscale": tailscale_ips(),
        "port": PORT,
        "cdp": bool(agent.get("cdp")),
        "agent": {
            "ok": bool(agent.get("ok")),
            "cdp": bool(agent.get("cdp")),
            "status": agent.get("status"),
            "workspace": agent.get("workspace"),
            "error": agent.get("error"),
        },
    }


@app.get("/api/agent-image", response_model=None)
async def agent_image(src: str):
    result = await AGENT.fetch_image(src)
    if not result.get("ok"):
        return JSONResponse(
            {"ok": False, "error": result.get("error") or "image unavailable"},
            status_code=404,
        )
    try:
        content = base64.b64decode(str(result.get("data") or ""), validate=True)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid image data"}, status_code=502)
    return Response(
        content=content,
        media_type=str(result.get("mime") or "image/png"),
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/api/files")
def api_files(path: str | None = None) -> JSONResponse:
    return JSONResponse(file_browser.list_dir(path))


@app.get("/api/file", response_model=None)
def api_file(path: str):
    resolved, err = file_browser.file_response_path(path)
    if err or resolved is None:
        return JSONResponse({"ok": False, "error": err or "missing"}, status_code=404)
    return FileResponse(
        path=str(resolved),
        media_type=file_browser.guess_media_type(resolved),
        filename=resolved.name,
    )


@app.post("/api/explorer")
async def api_explorer(request: Request) -> JSONResponse:
    payload = await request.json()
    return JSONResponse(file_browser.open_explorer(str(payload.get("path") or "")))


@app.post("/api/open")
async def api_open(request: Request) -> JSONResponse:
    payload = await request.json()
    return JSONResponse(file_browser.open_on_pc(str(payload.get("path") or "")))


@app.post("/api/reload-server")
def api_reload_server() -> JSONResponse:
    """Restart host process — Tailscale URL stays the same."""
    bat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Restart-Capture-Only.bat")
    try:
        subprocess.Popen(
            ["cmd", "/c", bat],
            cwd=os.path.dirname(bat),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        return JSONResponse({"ok": True, "message": "capture restart launched"})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


def restart_capture() -> dict:
    # Soft refresh only — do not resize or steal focus from Cursor.
    hwnd = find_cursor_hwnd()
    with HUB.lock:
        HUB.jpeg = b""
        HUB._seq += 1
        HUB.hwnd = hwnd
    return {"ok": True, "hwnd": int(hwnd or 0)}


@app.post("/restart")
def restart_endpoint() -> dict:
    return restart_capture()


@app.get("/restart")
def restart_endpoint_get() -> dict:
    return restart_capture()


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket) -> None:
    await ws.accept()
    q = AGENT.subscribe()
    try:
        await ws.send_text(json.dumps({"type": "state", "state": AGENT.state}))

        async def reader() -> None:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                typ = msg.get("type")
                if typ == "prompt":
                    result = await AGENT.prompt(
                        str(msg.get("text") or ""),
                        msg.get("images") if isinstance(msg.get("images"), list) else [],
                    )
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "approve":
                    result = await AGENT.approve()
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "reject":
                    result = await AGENT.reject()
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "new_chat":
                    result = await AGENT.new_chat()
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "select_tab":
                    result = await AGENT.select_tab(str(msg.get("id") or ""))
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "switch_window":
                    result = await AGENT.switch_window(str(msg.get("id") or ""))
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "list_models":
                    result = await AGENT.list_models()
                    await ws.send_text(json.dumps({"type": "models", **result}))
                elif typ == "set_model":
                    result = await AGENT.set_model(str(msg.get("model") or msg.get("id") or ""))
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "set_mode":
                    result = await AGENT.set_mode(str(msg.get("mode") or msg.get("id") or ""))
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "expand_activity":
                    result = await AGENT.expand_activity(str(msg.get("id") or ""))
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "edit_message":
                    result = await AGENT.edit_message(
                        str(msg.get("id") or ""),
                        str(msg.get("text") or ""),
                    )
                    await ws.send_text(json.dumps({"type": "result", **result}))
                elif typ == "refresh":
                    state = await AGENT.refresh_state()
                    await ws.send_text(json.dumps({"type": "state", "state": state}))

        read_task = asyncio.create_task(reader())
        try:
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg))
        finally:
            read_task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        AGENT.unsubscribe(q)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    HUB.viewers += 1
    last_seq = -1
    try:
        await ws.send_text(
            json.dumps(
                {
                    "type": "status",
                    "text": "Cursor window" if HUB.hwnd else "waiting for Cursor…",
                }
            )
        )

        async def reader() -> None:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                typ = msg.get("type")
                if typ == "pong":
                    continue
                if typ == "preset":
                    name = str(msg.get("name") or "balanced")
                    preset = PRESETS.get(name) or PRESETS["balanced"]
                    with _settings_lock:
                        _settings.update(preset)
                        _settings["preset"] = name
                    continue
                if typ == "restart":
                    info = restart_capture()
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "status",
                                "text": f"restarted hwnd={info.get('hwnd')}",
                            }
                        )
                    )
                    continue

                hwnd = HUB.hwnd or find_cursor_hwnd()
                if not hwnd:
                    continue
                if typ in ("down", "up", "move"):
                    x, y = client_to_screen(
                        hwnd, float(msg.get("x", 0)), float(msg.get("y", 0))
                    )
                    button = str(msg.get("button") or "left")
                    if typ == "down":
                        mouse_down(x, y, button)
                    elif typ == "up":
                        mouse_up(x, y, button)
                    else:
                        mouse_move(x, y)
                elif typ == "wheel":
                    wx = wy = None
                    if msg.get("x") is not None and msg.get("y") is not None:
                        wx, wy = client_to_screen(
                            hwnd, float(msg.get("x", 0)), float(msg.get("y", 0))
                        )
                    mouse_wheel(int(msg.get("dy") or 0), wx, wy)
                elif typ == "text":
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        pass
                    type_text(str(msg.get("text") or ""))

        read_task = asyncio.create_task(reader())
        last_ping = 0.0
        try:
            while True:
                now = time.time()
                if now - last_ping >= 10.0:
                    await ws.send_text(json.dumps({"type": "ping", "t": int(now * 1000)}))
                    last_ping = now
                with HUB.lock:
                    frame = HUB.jpeg
                    seq = HUB._seq
                if frame and seq != last_seq:
                    await ws.send_bytes(frame)
                    last_seq = seq
                    if seq % 15 == 0:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "stats",
                                    "text": "Cursor window"
                                    if HUB.hwnd
                                    else "waiting for Cursor…",
                                    "encode_ms": round(HUB.encode_ms, 1),
                                }
                            )
                        )
                await asyncio.sleep(0.008)
        finally:
            read_task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        HUB.viewers = max(0, HUB.viewers - 1)


def main() -> None:
    local = lan_ips() or ["127.0.0.1"]
    ts = tailscale_ips()
    print("")
    print("############################################################")
    print("#  PHONE URL (Tailscale) - open this on your phone         #")
    print("############################################################")
    if ts:
        for ip in ts:
            print(f"")
            print(f"    http://{ip}:{PORT}")
        print("")
        print("  Phone + PC both on Tailscale (same account).")
    else:
        print("")
        print("    Tailscale IP not available yet.")
        print("    Sign in to Tailscale on this PC, then restart host.")
    print("############################################################")
    print("")
    print("Same Wi-Fi (optional):")
    for ip in local:
        if ip not in ts:
            print(f"  http://{ip}:{PORT}")
    print("Ctrl+C to stop.\n")

    # Stable by default: a file watcher can restart against half-written multi-file
    # updates. Opt in only for deliberate local development; production updates use
    # Restart-Capture-Only.bat once all files and checks are complete.
    reload = os.environ.get("CURSORDESK_RELOAD", "0") in ("1", "true", "True")
    if reload:
        uvicorn.run(
            "cursor_window_stream:app",
            host="0.0.0.0",
            port=PORT,
            reload=True,
            reload_dirs=[os.path.dirname(os.path.abspath(__file__))],
            log_level="warning",
        )
    else:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
