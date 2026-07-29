"""CDP bridge into local Cursor — agent chat, approvals, prompts (no screen capture)."""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import file_browser
import websockets
from image_resolver import allowed_image_paths, enrich_state_images, normalize_path
from websockets.protocol import State

CDP_BASE = os.environ.get("CURSORDESK_CDP", "http://127.0.0.1:9222").rstrip("/")
POLL_MS = float(os.environ.get("CURSORDESK_AGENT_POLL_MS", "500"))

INPUT_SELECTORS = [
    "#workbench\\.parts\\.auxiliarybar [contenteditable='true']",
    "#workbench\\.parts\\.auxiliarybar textarea",
    "#workbench\\.parts\\.auxiliarybar [role='textbox']",
    ".composer-bar [contenteditable='true']",
    ".composer-bar textarea",
    "[contenteditable='true']",
    "textarea",
]

APPROVE_SELECTORS = [
    "button.ui-shell-tool-call__run-btn",
    "button.ui-shell-tool-call__allowlist-button",
    "button[aria-label*='Accept']",
    "button[aria-label*='Approve']",
    "button[aria-label*='Run']",
    "button[aria-label*='Allow']",
    ".composer-run-button",
    ".composer-create-plan-build-button",
]

REJECT_SELECTORS = [
    "button.ui-shell-tool-call__skip-btn",
    "button[aria-label*='Reject']",
    "button[aria-label*='Deny']",
    "button[aria-label*='Cancel']",
    ".composer-skip-button",
]

APPROVE_TEXT = ["Accept All", "Accept", "Approve", "Run", "Allow", "Build"]
REJECT_TEXT = ["Reject", "Deny", "Cancel", "Skip"]

# Runs inside Cursor's renderer via Runtime.evaluate — must be self-contained.
def load_extract_js() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp_extract.js")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


EXTRACT_JS = load_extract_js()


def _http_json(url: str, timeout: float = 2.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def list_cdp_targets() -> list[dict]:
    try:
        data = _http_json(f"{CDP_BASE}/json")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def pick_workbench_target(targets: list[dict]) -> Optional[dict]:
    pages = [t for t in targets if (t.get("type") == "page" or t.get("webSocketDebuggerUrl"))]
    workbenches = [
        t
        for t in pages
        if "workbench" in (t.get("url") or "").lower()
        or "workbench" in (t.get("title") or "").lower()
    ]
    if workbenches:
        # Prefer largest / most recently listed Cursor window
        return workbenches[0]
    # Fallback: any page with Cursor in title
    for t in pages:
        title = (t.get("title") or "").lower()
        if "cursor" in title and "devtools" not in title:
            return t
    return pages[0] if pages else None


class CdpClient:
    def __init__(self) -> None:
        self._ws: Any = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self.target_id: str = ""
        self.target_title: str = ""

    @property
    def connected(self) -> bool:
        # websockets 13+ uses .state (State.OPEN); older used .open
        if self._ws is None:
            return False
        try:
            return self._ws.state is State.OPEN
        except Exception:
            return bool(getattr(self._ws, "open", False))

    async def connect(self, ws_url: str, target_id: str = "", title: str = "") -> None:
        await self.disconnect()
        self._ws = await websockets.connect(
            ws_url,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=2,
        )
        self.target_id = target_id
        self.target_title = title
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await self.send("Runtime.enable")
        except Exception:
            pass

    async def disconnect(self) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("CDP disconnected"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue
                fut = self._pending.pop(int(mid), None)
                if not fut or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(RuntimeError(msg["error"].get("message") or str(msg["error"])))
                else:
                    fut.set_result(msg.get("result") or {})
        except Exception:
            pass
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("CDP disconnected"))
            self._pending.clear()

    async def send(self, method: str, params: Optional[dict] = None, timeout: float = 8.0) -> dict:
        if not self._ws:
            raise RuntimeError("CDP not connected")
        mid = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        payload: dict[str, Any] = {"id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        await self._ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=timeout)

    async def evaluate(self, expression: str, timeout: float = 8.0) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            ed = result["exceptionDetails"]
            exc = (ed.get("exception") or {}).get("description") or ed.get("text") or "eval failed"
            raise RuntimeError(exc)
        remote = result.get("result") or {}
        return remote.get("value")

    async def click_selector(self, selector: str) -> bool:
        return bool(
            await self.evaluate(
                f"""(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return false;
  el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
  el.click();
  return true;
}})()"""
            )
        )

    async def click_by_label(self, labels: list[str]) -> bool:
        return bool(
            await self.evaluate(
                f"""(() => {{
  const labels = {json.dumps(labels)};
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const nodes = document.querySelectorAll('button, [role="button"]');
  for (const want of labels) {{
    const w = norm(want);
    for (const el of nodes) {{
      const label = norm(el.getAttribute('aria-label') || el.innerText || el.textContent || '');
      if (!label) continue;
      if (label === w || label.startsWith(w + ' ') || label.includes(w)) {{
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
        el.click();
        return true;
      }}
    }}
  }}
  return false;
}})()"""
            )
        )

    async def press_key(
        self, key: str, code: str, key_code: int, modifiers: int = 0
    ) -> None:
        base = {
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": key_code,
            "nativeVirtualKeyCode": key_code,
            "modifiers": modifiers,
        }
        await self.send("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    async def insert_text(self, text: str) -> None:
        await self.send("Input.insertText", {"text": text})


class AgentHub:
    def __init__(self) -> None:
        self.client = CdpClient()
        self.state: dict[str, Any] = {
            "ok": False,
            "cdp": False,
            "error": "starting",
            "messages": [],
            "approvals": [],
            "rejects": [],
            "updatedAt": 0,
        }
        self._subs: set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self._cmd_lock = asyncio.Lock()
        self._last_fingerprint = ""
        self._was_loading = False
        self._selected_tab_id = ""
        self._selected_tab_title = ""
        self.usage_stats: dict[str, Any] = {
            "prompts": 0,
            "approvals": 0,
            "rejects": 0,
            "modelChanges": 0,
            "byModel": {},
            "startedAt": time.time(),
        }

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        await self.client.disconnect()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.add(q)
        try:
            q.put_nowait({"type": "state", "state": self.state})
        except asyncio.QueueFull:
            pass
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def _broadcast(self, msg: dict) -> None:
        dead: list[asyncio.Queue] = []
        for q in list(self._subs):
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subs.discard(q)

    async def ensure_connected(self) -> bool:
        if self.client.connected:
            return True
        targets = await asyncio.to_thread(list_cdp_targets)
        target = pick_workbench_target(targets)
        if not target:
            self.state = {
                "ok": False,
                "cdp": False,
                "error": (
                    "Cursor CDP not found on :9222. Fully quit Cursor, then relaunch with "
                    "--remote-debugging-port=9222 (Host\\Start-CursorDesk.bat -Relaunch)."
                ),
                "messages": [],
                "approvals": [],
                "rejects": [],
                "targets": len(targets),
                "updatedAt": time.time(),
            }
            return False
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            self.state["error"] = "target has no webSocketDebuggerUrl"
            self.state["cdp"] = False
            self.state["ok"] = False
            return False
        try:
            await self.client.connect(
                ws_url,
                target_id=str(target.get("id") or ""),
                title=str(target.get("title") or ""),
            )
        except Exception as exc:
            self.state = {
                "ok": False,
                "cdp": False,
                "error": f"CDP connect failed: {exc}",
                "messages": [],
                "approvals": [],
                "rejects": [],
                "updatedAt": time.time(),
            }
            return False
        return True

    async def refresh_state(self) -> dict:
        if not await self.ensure_connected():
            await self._broadcast({"type": "state", "state": self.state})
            return self.state
        try:
            extracted = await self.client.evaluate(EXTRACT_JS, timeout=6.0)
        except Exception as exc:
            await self.client.disconnect()
            self.state = {
                "ok": False,
                "cdp": False,
                "error": f"extract failed: {exc}",
                "messages": [],
                "approvals": [],
                "rejects": [],
                "updatedAt": time.time(),
            }
            await self._broadcast({"type": "state", "state": self.state})
            return self.state

        if not isinstance(extracted, dict):
            extracted = {"ok": False, "error": "empty extract"}
        if self._selected_tab_id:
            for tab in extracted.get("tabs") or []:
                tab["active"] = (
                    str(tab.get("id") or "") == self._selected_tab_id
                    or str(tab.get("title") or "") == self._selected_tab_title
                )
            for group in extracted.get("repos") or []:
                for chat in group.get("chats") or []:
                    chat["active"] = (
                        str(chat.get("id") or "") == self._selected_tab_id
                        or str(chat.get("title") or "") == self._selected_tab_title
                    )

        loading = bool(extracted.get("loading"))
        just_done = self._was_loading and not loading
        self._was_loading = loading

        model = str(extracted.get("model") or "")
        if model:
            by = self.usage_stats["byModel"]
            by.setdefault(model, int(by.get(model) or 0))

        self.state = {
            **extracted,
            "ok": bool(extracted.get("ok", True)),
            "cdp": True,
            "error": extracted.get("error"),
            "targetTitle": self.client.target_title,
            "windows": self.list_windows(),
            "usageStats": dict(self.usage_stats),
            "updatedAt": time.time(),
        }
        enrich_state_images(self.state)
        fp = json.dumps(
            {
                "m": [
                    (m.get("id"), m.get("text", "")[:80], len(m.get("images") or []))
                    for m in self.state.get("messages") or []
                ],
                "a": self.state.get("approvals"),
                "t": [
                    (t.get("id"), t.get("title"), t.get("active"), t.get("working"))
                    for t in self.state.get("tabs") or []
                ],
                "q": self.state.get("queue"),
                "act": [(x.get("type"), (x.get("text") or "")[:60]) for x in (self.state.get("activity") or [])[-12:]],
                "s": self.state.get("status"),
                "mode": self.state.get("mode"),
                "model": self.state.get("model"),
                "loading": self.state.get("loading"),
                "session": self.state.get("session"),
                "usage": self.state.get("usage"),
            },
            sort_keys=True,
        )
        if fp != self._last_fingerprint:
            self._last_fingerprint = fp
            await self._broadcast({"type": "state", "state": self.state})
        if just_done:
            await self._broadcast(
                {
                    "type": "done",
                    "text": self.state.get("status") or "Agent finished",
                    "workspace": self.state.get("workspace"),
                    "model": self.state.get("model"),
                }
            )
        return self.state

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.refresh_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state = {
                    "ok": False,
                    "cdp": False,
                    "error": str(exc),
                    "messages": [],
                    "approvals": [],
                    "rejects": [],
                    "updatedAt": time.time(),
                }
                await self._broadcast({"type": "state", "state": self.state})
            await asyncio.sleep(max(0.2, POLL_MS / 1000.0))

    async def prompt(self, text: str, images: Optional[list[dict]] = None) -> dict:
        text = (text or "").strip()
        clean_images: list[dict[str, str]] = []
        total_chars = 0
        for image in images or []:
            mime = str(image.get("mime") or "").lower()
            data = str(image.get("data") or "")
            name = os.path.basename(str(image.get("name") or "image"))
            if not mime.startswith("image/") or not data:
                continue
            total_chars += len(data)
            if total_chars > 11_300_000:
                return {"ok": False, "error": "images are too large (8 MB total maximum)"}
            clean_images.append({"mime": mime, "data": data, "name": name[:120]})
        if not text and not clean_images:
            return {"ok": False, "error": "empty prompt"}
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            focused = await self.client.evaluate(
                f"""(() => {{
  const strategies = {json.dumps(INPUT_SELECTORS)};
  let input = null;
  for (const sel of strategies) {{
    try {{ input = document.querySelector(sel); if (input) break; }} catch (e) {{}}
  }}
  if (!input) return false;
  input.scrollIntoView({{ block: 'center', behavior: 'instant' }});
  input.focus();
  input.click();
  return true;
}})()"""
            )
            if not focused:
                return {"ok": False, "error": "chat input not found — open Agents chat in Cursor"}
            await asyncio.sleep(0.1)
            await self.client.press_key("a", "KeyA", 65, modifiers=2)
            await asyncio.sleep(0.04)
            await self.client.press_key("Backspace", "Backspace", 8)
            await asyncio.sleep(0.04)
            if text:
                await self.client.insert_text(text)
            if clean_images:
                attached = await self.client.evaluate(
                    f"""(async () => {{
  const strategies = {json.dumps(INPUT_SELECTORS)};
  const images = {json.dumps(clean_images)};
  let input = null;
  for (const sel of strategies) {{
    try {{ input = document.querySelector(sel); if (input) break; }} catch (e) {{}}
  }}
  if (!input) return {{ ok: false, error: 'chat input not found' }};
  const files = [];
  for (const image of images) {{
    const raw = atob(image.data);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    files.push(new File([bytes], image.name || 'image', {{ type: image.mime }}));
  }}
  const fileInput = Array.from(document.querySelectorAll('input[type="file"]')).find((el) =>
    !el.disabled && (!el.accept || /image|\\*/i.test(el.accept))
  );
  if (fileInput) {{
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    fileInput.files = transfer.files;
    fileInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
    fileInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
    await new Promise((resolve) => setTimeout(resolve, 180));
    return {{ ok: true, count: files.length, via: 'file-input' }};
  }}
  let count = 0;
  for (const file of files) {{
    const transfer = new DataTransfer();
    transfer.items.add(file);
    const event = new ClipboardEvent('paste', {{
      bubbles: true,
      cancelable: true,
      clipboardData: transfer,
    }});
    input.dispatchEvent(event);
    count++;
    await new Promise((resolve) => setTimeout(resolve, 120));
  }}
  return {{ ok: true, count, via: 'paste' }};
}})()""",
                    timeout=20.0,
                )
                if not attached or not attached.get("ok"):
                    return {
                        "ok": False,
                        "error": (attached or {}).get("error") or "could not attach image",
                    }
            await asyncio.sleep(0.12)
            await self.client.press_key("Enter", "Enter", 13)
            await asyncio.sleep(0.25)
            still = await self.client.evaluate(
                f"""(() => {{
  const strategies = {json.dumps(INPUT_SELECTORS)};
  const typed = {json.dumps(text)};
  let input = null;
  for (const sel of strategies) {{
    try {{ input = document.querySelector(sel); if (input) break; }} catch (e) {{}}
  }}
  if (!input) return false;
  const cur = (input.isContentEditable
    ? (input.innerText || input.textContent || '')
    : (input.value || input.innerText || '')).trim();
  return cur.length > 0 && cur.includes(typed);
}})()"""
            )
            if still:
                await self.client.press_key("Enter", "Enter", 13, modifiers=2)
            self.usage_stats["prompts"] = int(self.usage_stats.get("prompts") or 0) + 1
            model_name = str(self.state.get("model") or "")
            if model_name:
                by = self.usage_stats["byModel"]
                by[model_name] = int(by.get(model_name) or 0) + 1
            await self.refresh_state()
            return {"ok": True}

    async def fetch_local_image(self, path: str) -> dict:
        import base64

        path = normalize_path(path)
        allowed = allowed_image_paths(self.state)
        if not path or path not in allowed:
            return {"ok": False, "error": "image is not in the current chat"}
        resolved, err = file_browser.file_response_path(path)
        if err or resolved is None:
            return {"ok": False, "error": err or "file not allowed"}
        try:
            data = base64.b64encode(resolved.read_bytes()).decode("ascii")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "mime": file_browser.guess_media_type(resolved),
            "data": data,
        }

    async def fetch_image(self, src: str) -> dict:
        src = str(src or "")
        allowed = {
            str(image.get("src") or "")
            for message in self.state.get("messages") or []
            for image in message.get("images") or []
        }
        local_allowed = {
            f"local:{path}" for path in allowed_image_paths(self.state)
        }
        if not src or (src not in allowed and src not in local_allowed):
            return {"ok": False, "error": "image is not in the current chat"}
        if src.startswith("local:"):
            return await self.fetch_local_image(src[6:])
        local = normalize_path(src)
        if local and local in allowed_image_paths(self.state):
            return await self.fetch_local_image(local)
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            result = await self.client.evaluate(
                f"""(async () => {{
  const response = await fetch({json.dumps(src)});
  if (!response.ok) throw new Error(`image request failed: ${{response.status}}`);
  const blob = await response.blob();
  if (!blob.type.startsWith('image/')) throw new Error('resource is not an image');
  if (blob.size > 12 * 1024 * 1024) throw new Error('image exceeds 12 MB');
  const data = await new Promise((resolve, reject) => {{
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  }});
  return {{ ok: true, mime: blob.type || 'image/png', data }};
}})()""",
                timeout=20.0,
            )
            return result if isinstance(result, dict) else {"ok": False, "error": "image read failed"}

    async def approve(self) -> dict:
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            for sel in APPROVE_SELECTORS:
                try:
                    if await self.client.click_selector(sel):
                        await asyncio.sleep(0.2)
                        await self.refresh_state()
                        return {"ok": True, "via": sel}
                except Exception:
                    continue
            if await self.client.click_by_label(APPROVE_TEXT):
                await asyncio.sleep(0.2)
                await self.refresh_state()
                return {"ok": True, "via": "label"}
            return {"ok": False, "error": "no approve/accept button found"}

    async def reject(self) -> dict:
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            for sel in REJECT_SELECTORS:
                try:
                    if await self.client.click_selector(sel):
                        await asyncio.sleep(0.2)
                        await self.refresh_state()
                        return {"ok": True, "via": sel}
                except Exception:
                    continue
            if await self.client.click_by_label(REJECT_TEXT):
                await asyncio.sleep(0.2)
                await self.refresh_state()
                return {"ok": True, "via": "label"}
            return {"ok": False, "error": "no reject/skip button found"}

    async def new_chat(self) -> dict:
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            sels = [
                '[data-command-id="composer.createNewComposerTab"]',
                '[aria-label="New Agent"]',
                '[aria-label*="New Agent"]',
                '[aria-label="New Chat"]',
                '[aria-label*="New Chat"]',
                '[aria-label*="New chat"]',
                "a.codicon-add-two",
            ]
            for sel in sels:
                try:
                    if await self.client.click_selector(sel):
                        await asyncio.sleep(0.35)
                        await self.refresh_state()
                        return {"ok": True, "via": sel}
                except Exception:
                    continue
            if await self.client.click_by_label(["New Agent", "New Chat", "New chat"]):
                await asyncio.sleep(0.35)
                await self.refresh_state()
                return {"ok": True, "via": "label"}
            return {"ok": False, "error": "New Agent control not found — open Agents window in Cursor"}

    async def select_tab(self, tab_id: str) -> dict:
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            tab_id = str(tab_id or "")
            if not tab_id:
                return {"ok": False, "error": "missing conversation id"}
            point = await self.client.evaluate(
                f"""(() => {{
  const id = {json.dumps(tab_id)};
  let el = document.getElementById(id);
  if (el && !el.classList.contains('glass-sidebar-agent-menu-btn')) el = null;
  if (!el && id.startsWith('index:')) {{
    const index = Number(id.slice(6));
    el = document.querySelectorAll('.glass-sidebar-agent-menu-btn')[index] || null;
  }}
  if (!el) return {{ ok: false, error: 'conversation row no longer exists' }};
  const rawTitle = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  const title = rawTitle.replace(/\\s+(\\d+\\s*[smhd]|just now|now)$/i, '').trim() || rawTitle;
  el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
  const r = el.getBoundingClientRect();
  const x = r.left + Math.min(r.width * .35, 120);
  const y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y)?.closest('.glass-sidebar-agent-menu-btn');
  if (!hit || hit.id !== el.id) return {{ ok: false, error: 'conversation row is not clickable' }};
  return {{ ok: true, title, x, y }};
}})()"""
            )
            if not point or not point.get("ok"):
                return {
                    "ok": False,
                    "error": (point or {}).get("error") or "conversation not found",
                }
            mouse = {
                "x": float(point["x"]),
                "y": float(point["y"]),
                "button": "left",
                "clickCount": 1,
            }
            await self.client.send(
                "Input.dispatchMouseEvent", {"type": "mousePressed", **mouse}
            )
            await self.client.send(
                "Input.dispatchMouseEvent", {"type": "mouseReleased", **mouse}
            )
            self._selected_tab_id = tab_id
            self._selected_tab_title = str(point.get("title") or "")
            # Conversation contents render asynchronously; waiting here prevents
            # an immediate refresh from sending the previous transcript.
            await asyncio.sleep(0.8)
            await self.refresh_state()
            return {"ok": True, "title": point.get("title") or ""}

    def list_windows(self) -> list[dict]:
        out = []
        for t in list_cdp_targets():
            url = (t.get("url") or "").lower()
            title = t.get("title") or ""
            if "devtools" in title.lower():
                continue
            if t.get("type") not in (None, "page", "app"):
                continue
            if "workbench" not in url and "cursor" not in title.lower():
                continue
            out.append(
                {
                    "id": str(t.get("id") or ""),
                    "title": title,
                    "active": str(t.get("id") or "") == self.client.target_id,
                }
            )
        return out

    async def switch_window(self, target_id: str) -> dict:
        async with self._cmd_lock:
            targets = list_cdp_targets()
            target = next((t for t in targets if str(t.get("id")) == str(target_id)), None)
            if not target or not target.get("webSocketDebuggerUrl"):
                return {"ok": False, "error": "window not found"}
            await self.client.disconnect()
            try:
                await self.client.connect(
                    target["webSocketDebuggerUrl"],
                    target_id=str(target.get("id") or ""),
                    title=str(target.get("title") or ""),
                )
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            await self.refresh_state()
            return {"ok": True}

    async def list_models(self) -> dict:
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp", "models": []}
            await self.client.press_key("Escape", "Escape", 27)
            await asyncio.sleep(0.1)
            opened = await self.client.evaluate(
                """(() => {
  const btn = document.querySelector('.ui-model-picker__trigger, .glass-model-picker-wrapper button, .glass-model-picker-wrapper');
  if (!btn) return false;
  (btn.closest('button') || btn).click();
  return true;
})()"""
            )
            if not opened:
                return {"ok": False, "error": "model picker not found", "models": []}
            await asyncio.sleep(0.35)
            models = await self.client.evaluate(
                """(() => {
  const menus = [...document.querySelectorAll('.ui-menu, [role=menu]')].filter(m => {
    const r = m.getBoundingClientRect();
    return r.width > 80 && r.height > 40 && /Auto|Grok|Composer|model/i.test(m.innerText || '');
  });
  const out = [];
  const seen = new Set();
  for (const menu of menus) {
    for (const el of menu.querySelectorAll('button, [role=menuitem], [class*=menu-item]')) {
      const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
      if (!t || t.length > 90) continue;
      if (/^edit$/i.test(t)) continue;
      const key = t.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      const label = t.split('  ')[0].split(' Balanced')[0].trim();
      let group = 'model';
      if (/^cursor models$/i.test(label)) group = 'header';
      if (/^other models$/i.test(label)) group = 'other';
      out.push({
        id: 'label::' + label,
        label,
        detail: t,
        selected: /selected|active|aria-checked=\"true\"/i.test(
          (el.className || '') + (el.getAttribute('aria-checked') || '')
        ),
        group,
      });
    }
  }
  return out;
})()"""
            )
            await self.client.press_key("Escape", "Escape", 27)
            await asyncio.sleep(0.1)
            return {"ok": True, "models": models or []}

    async def set_model(self, model_label: str) -> dict:
        label = (model_label or "").strip()
        if not label:
            return {"ok": False, "error": "empty model"}
        if label.startswith("label::"):
            label = label[7:]
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            await self.client.press_key("Escape", "Escape", 27)
            await asyncio.sleep(0.1)
            opened = await self.client.evaluate(
                """(() => {
  const btn = document.querySelector('.ui-model-picker__trigger, .glass-model-picker-wrapper button, .glass-model-picker-wrapper');
  if (!btn) return false;
  (btn.closest('button') || btn).click();
  return true;
})()"""
            )
            if not opened:
                return {"ok": False, "error": "model picker not found"}
            await asyncio.sleep(0.3)
            clicked = await self.client.evaluate(
                f"""(() => {{
  const want = {json.dumps(label)}.toLowerCase();
  const menus = [...document.querySelectorAll('.ui-menu, [role=menu]')].filter(m => {{
    const r = m.getBoundingClientRect();
    return r.width > 80 && r.height > 40;
  }});
  const clickRow = (el) => {{
    (el.querySelector('button, [role=menuitem]') || el).click();
  }};
  for (const menu of menus) {{
    for (const el of menu.querySelectorAll('button, [role=menuitem], [class*=menu-item]')) {{
      const t = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      if (!t) continue;
      if (t === want || t.startsWith(want) || t.includes(want)) {{
        clickRow(el);
        return {{ ok: true, via: t }};
      }}
    }}
  }}
  for (const menu of menus) {{
    for (const el of menu.querySelectorAll('button, [role=menuitem]')) {{
      if (/other models/i.test(el.innerText || '')) {{
        el.click();
        return {{ ok: false, needOther: true }};
      }}
    }}
  }}
  return {{ ok: false }};
}})()"""
            )
            if isinstance(clicked, dict) and clicked.get("needOther"):
                await asyncio.sleep(0.35)
                clicked = await self.client.evaluate(
                    f"""(() => {{
  const want = {json.dumps(label)}.toLowerCase();
  const menus = [...document.querySelectorAll('.ui-menu, [role=menu]')].filter(m => {{
    const r = m.getBoundingClientRect();
    return r.width > 80 && r.height > 40;
  }});
  for (const menu of menus) {{
    for (const el of menu.querySelectorAll('button, [role=menuitem], [class*=menu-item]')) {{
      const t = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      if (t === want || t.startsWith(want) || t.includes(want)) {{
        (el.querySelector('button,[role=menuitem]') || el).click();
        return {{ ok: true, via: t }};
      }}
    }}
  }}
  return {{ ok: false }};
}})()"""
                )
            await asyncio.sleep(0.2)
            await self.client.press_key("Escape", "Escape", 27)
            if not (isinstance(clicked, dict) and clicked.get("ok")):
                return {"ok": False, "error": f'model "{label}" not found'}
            self.usage_stats["modelChanges"] = int(self.usage_stats.get("modelChanges") or 0) + 1
            await self.refresh_state()
            return {"ok": True, "model": self.state.get("model")}

    async def set_mode(self, mode_id: str) -> dict:
        mode = (mode_id or "").strip().lower()
        if not mode:
            return {"ok": False, "error": "empty mode"}
        aliases = {
            "agent": "agent",
            "plan": "plan",
            "ask": "ask",
            "debug": "debug",
            "multitask": "triage",
            "triage": "triage",
            "edit": "edit",
        }
        mode = aliases.get(mode, mode)
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            await self.client.evaluate(
                """(() => {
  const sels = [
    '.composer-unified-dropdown[data-mode]',
    '.composer-bar-input-buttons[data-mode]',
    '[data-mode].composer-unified-dropdown',
    'button[aria-label*="Mode"]',
  ];
  for (const sel of sels) {
    try {
      const el = document.querySelector(sel);
      if (el) { el.click(); return true; }
    } catch (e) {}
  }
  return false;
})()"""
            )
            await asyncio.sleep(0.25)
            selected = await self.client.evaluate(
                f"""(() => {{
  const modeId = {json.dumps(mode)};
  const byId = document.querySelectorAll('[id*="composer-mode-"][id$="-' + modeId + '"]');
  for (const item of byId) {{
    (item.querySelector('.composer-unified-context-menu-item') || item).click();
    return true;
  }}
  const want = modeId === 'triage' ? ['multitask', 'triage'] : [modeId];
  for (const el of document.querySelectorAll('[role=menuitem], button, [class*=menu-item]')) {{
    const t = (el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!t || t.length > 40) continue;
    if (want.some(w => t === w || t.startsWith(w + ' '))) {{
      el.click();
      return true;
    }}
  }}
  return false;
}})()"""
            )
            await self.client.press_key("Escape", "Escape", 27)
            if not selected:
                return {
                    "ok": False,
                    "error": (
                        f'mode "{mode}" not found in this Cursor surface '
                        "(Agents window may hide mode controls)"
                    ),
                }
            await asyncio.sleep(0.2)
            await self.refresh_state()
            return {"ok": True, "mode": self.state.get("mode") or mode}

    async def expand_activity(self, activity_id: str) -> dict:
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            try:
                idx = int(activity_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "bad activity id"}
            ok = await self.client.evaluate(
                f"""(() => {{
  const list = [...document.querySelectorAll('.ui-collapsible-header')];
  const el = list[{idx}];
  if (!el) return false;
  el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
  el.click();
  return true;
}})()"""
            )
            await asyncio.sleep(0.25)
            await self.refresh_state()
            return {"ok": bool(ok)}

    async def edit_message(self, message_id: str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty text"}
        async with self._cmd_lock:
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            opened = await self.client.evaluate(
                f"""(() => {{
  const id = {json.dumps(str(message_id))};
  let el = document.querySelector('[data-message-id="' + id + '"]')
    || document.querySelector('[data-server-bubble-id="' + id + '"]')
    || document.getElementById('bubble-' + id)
    || document.querySelector('[data-message-index="' + id + '"]');
  if (!el) {{
    const humans = [...document.querySelectorAll('[data-message-role="human"]')];
    el = humans[humans.length - 1] || null;
  }}
  if (!el) return {{ ok: false, error: 'message not found' }};
  el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
  el.dispatchEvent(new MouseEvent('mouseover', {{ bubbles: true }}));
  el.click();
  const editBtn = el.querySelector('button[aria-label*="Edit"], button[aria-label*="edit"]')
    || [...document.querySelectorAll('button')].find(b =>
      /^(edit|edit message)$/i.test((b.getAttribute('aria-label') || b.innerText || '').trim())
    );
  if (editBtn) {{ editBtn.click(); return {{ ok: true, via: 'edit-button' }}; }}
  const content = el.querySelector('.aislash-editor-input-readonly, [contenteditable]') || el;
  content.dispatchEvent(new MouseEvent('dblclick', {{ bubbles: true }}));
  return {{ ok: true, via: 'dblclick' }};
}})()"""
            )
            await asyncio.sleep(0.35)
            filled = await self.client.evaluate(
                f"""(() => {{
  const strategies = {json.dumps(INPUT_SELECTORS)};
  let input = null;
  for (const sel of strategies) {{
    try {{ input = document.querySelector(sel); if (input) break; }} catch (e) {{}}
  }}
  if (!input) return false;
  input.focus();
  input.click();
  return true;
}})()"""
            )
            if not filled:
                return {"ok": False, "error": "could not focus editor for edit", "opened": opened}
            await asyncio.sleep(0.08)
            await self.client.press_key("a", "KeyA", 65, modifiers=2)
            await asyncio.sleep(0.04)
            await self.client.press_key("Backspace", "Backspace", 8)
            await self.client.insert_text(text)
            await asyncio.sleep(0.12)
            await self.client.press_key("Enter", "Enter", 13)
            self.usage_stats["prompts"] = int(self.usage_stats.get("prompts") or 0) + 1
            await asyncio.sleep(0.2)
            await self.refresh_state()
            return {"ok": True, "opened": opened}



AGENT = AgentHub()
