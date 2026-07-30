"""CDP bridge into local Cursor — agent chat, approvals, prompts (no screen capture)."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import file_browser
import websockets
from image_resolver import allowed_image_paths, enrich_state_images, normalize_path
from websockets.protocol import State

CDP_BASE = os.environ.get("CURSORDESK_CDP", "http://127.0.0.1:9222").rstrip("/")
POLL_MS = float(os.environ.get("CURSORDESK_AGENT_POLL_MS", "500"))
HOST_DIR = Path(__file__).resolve().parent
OUTBOX_PATH = HOST_DIR / ".agent_outbox.json"
OUTBOX_FAILED_PATH = HOST_DIR / ".agent_outbox_failed.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically so a crash mid-write cannot leave truncated outbox data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _empty_subagents() -> dict[str, Any]:
    return {
        "conversation": {"id": "", "title": ""},
        "groups": [],
        "agents": [],
        "total": 0,
        "running": 0,
        "completed": 0,
        "error": 0,
        "selectedMatch": False,
    }


CONV_CACHE_MAX_TABS = 36
CONV_CACHE_MAX_MESSAGES = 240
OUTBOX_MAX_ATTEMPTS = 6
BG_CACHE_MIN_INTERVAL_S = 45.0
BG_CACHE_REFRESH_ENABLED = os.environ.get(
    "CURSORDESK_BG_CACHE_REFRESH", ""
).strip().lower() in {"1", "true", "yes", "on"}
# Retrying queued sends is on unless explicitly disabled; without it a message
# that fails once is never delivered and silently strands in the outbox.
OUTBOX_RETRY_ENABLED = os.environ.get(
    "CURSORDESK_OUTBOX_RETRY", "1"
).strip().lower() in {"1", "true", "yes", "on"}


class _ReentrantLock:
    """Async lock that a single task may acquire more than once.

    CDP command paths nest (prompt -> ensure conversation -> select tab), and a
    plain asyncio.Lock deadlocks there until the caller's timeout fires.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._depth = 0

    async def __aenter__(self) -> "_ReentrantLock":
        task = asyncio.current_task()
        if self._owner is not None and self._owner is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._owner = None
            self._depth = 0
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

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
    base = os.path.dirname(os.path.abspath(__file__))
    parts = []
    for name in ("subagent_parser.js", "cdp_extract.js"):
        with open(os.path.join(base, name), "r", encoding="utf-8") as f:
            parts.append(f.read())
    return "\n".join(parts)


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
            "subagents": _empty_subagents(),
            "approvals": [],
            "rejects": [],
            "updatedAt": 0,
        }
        self._subs: set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self._cmd_lock_obj: Optional["_ReentrantLock"] = None
        self._last_fingerprint = ""
        self._was_loading = False
        self._selected_tab_id = ""
        self._selected_tab_title = ""
        self._last_tab_resync = 0.0
        self._resyncing_tab = False
        self._tab_switch_deadline = 0.0
        self._outbox: list[dict[str, Any]] = []
        self._outbox_draining = False
        self._outbox_drain_task: Optional[asyncio.Task] = None
        self._conv_cache: dict[str, dict[str, Any]] = {}
        self._bg_cache_running = False
        self._last_bg_cache_at = 0.0
        self._outbox_drain_started_at = 0.0
        self._load_outbox()
        self.usage_stats: dict[str, Any] = {
            "prompts": 0,
            "approvals": 0,
            "rejects": 0,
            "modelChanges": 0,
            "byModel": {},
            "startedAt": time.time(),
        }

    def _cmd_lock(self) -> "_ReentrantLock":
        if self._cmd_lock_obj is None:
            self._cmd_lock_obj = _ReentrantLock()
        return self._cmd_lock_obj

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
        self._kick_outbox_drain()

    def _kick_outbox_drain(self) -> None:
        if not OUTBOX_RETRY_ENABLED:
            return
        if not self._outbox:
            return
        task = self._outbox_drain_task
        if task and not task.done():
            # Never spawn a second drainer; the old 120s flag reset could
            # duplicate-send the same item.
            return
        self._outbox_drain_task = asyncio.create_task(self._drain_outbox())

    def _load_outbox(self) -> None:
        try:
            if OUTBOX_PATH.is_file():
                raw = json.loads(OUTBOX_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._outbox = [item for item in raw if isinstance(item, dict)]
        except Exception:
            self._outbox = []

    def _save_outbox(self) -> bool:
        try:
            _atomic_write_json(OUTBOX_PATH, self._outbox)
            return True
        except Exception:
            return False

    def _update_conv_cache(self) -> None:
        # Never cache mid-switch: the transcript may still belong to the old chat.
        if time.time() < self._tab_switch_deadline:
            return
        tid = str(self._selected_tab_id or "")
        if not tid:
            return
        title = self._selected_tab_title
        msgs = self.state.get("messages") or []
        self._conv_cache[tid] = {
            "messages": copy.deepcopy(msgs[-CONV_CACHE_MAX_MESSAGES:]),
            "title": title,
            "updatedAt": time.time(),
        }
        if len(self._conv_cache) > CONV_CACHE_MAX_TABS:
            drop = sorted(
                self._conv_cache.items(),
                key=lambda item: float(item[1].get("updatedAt") or 0),
            )[: len(self._conv_cache) - CONV_CACHE_MAX_TABS]
            for key, _ in drop:
                self._conv_cache.pop(key, None)

    def conversation_cache(self, tab_id: str) -> dict[str, Any]:
        tab_id = str(tab_id or "")
        entry = self._conv_cache.get(tab_id)
        if not entry:
            return {"ok": False, "tabId": tab_id, "messages": []}
        return {
            "ok": True,
            "tabId": tab_id,
            "title": entry.get("title") or "",
            "messages": copy.deepcopy(entry.get("messages") or []),
            "updatedAt": entry.get("updatedAt"),
        }

    def all_conversation_caches(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for tab_id, entry in self._conv_cache.items():
            if not entry.get("messages"):
                continue
            out[str(tab_id)] = {
                "tabId": str(tab_id),
                "title": entry.get("title") or "",
                "messages": copy.deepcopy(entry.get("messages") or []),
                "updatedAt": entry.get("updatedAt"),
            }
        return {"ok": True, "caches": out}

    def _tab_title_for_id(self, tab_id: str) -> str:
        tab_id = str(tab_id or "")
        if not tab_id:
            return ""
        entry = self._conv_cache.get(tab_id)
        if entry and entry.get("title"):
            return str(entry.get("title") or "")
        for tab in self.state.get("tabs") or []:
            if str(tab.get("id") or "") == tab_id:
                return str(tab.get("title") or "")
        for group in self.state.get("repos") or []:
            for chat in group.get("chats") or []:
                if str(chat.get("id") or "") == tab_id:
                    return str(chat.get("title") or "")
        return ""

    def _stamp_subagent_scope(self, extracted: dict[str, Any]) -> None:
        """Keep scrape identity explicit; never relabel it as the intended chat."""
        raw = extracted.get("subagents")
        payload = raw if isinstance(raw, dict) else {}
        conversation = payload.get("conversation")
        conversation = conversation if isinstance(conversation, dict) else {}
        source_id = str(conversation.get("id") or "")
        source_title = str(conversation.get("title") or "")
        selected_id = str(self._selected_tab_id or "")
        selected_title = str(self._selected_tab_title or "")
        identity_known = bool(source_id or source_title)
        selected_known = bool(selected_id or selected_title)
        selected_match = identity_known and (
            bool(source_id and selected_id and source_id == selected_id)
            or bool(
                source_title
                and selected_title
                and self._titles_match(selected_title, source_title)
            )
        )
        # An unidentified transcript cannot safely own dock data. Clearing here
        # also guarantees an empty scrape replaces, rather than retains, history.
        if not identity_known:
            payload = {
                "conversation": {"id": "", "title": ""},
                "groups": [],
                "agents": [],
                "total": 0,
                "running": 0,
                "completed": 0,
                "error": 0,
            }
        payload["selectedMatch"] = selected_match if selected_known else False
        extracted["subagents"] = payload

    def _archive_failed_outbox_item(self, item: dict[str, Any], error: str) -> None:
        record = {
            **item,
            "failed_at": time.time(),
            "error": str(error or "send failed"),
        }
        failed: list[dict[str, Any]] = []
        try:
            if OUTBOX_FAILED_PATH.is_file():
                raw = json.loads(OUTBOX_FAILED_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    failed = [x for x in raw if isinstance(x, dict)]
        except Exception:
            failed = []
        failed.append(record)
        failed = failed[-24:]
        try:
            _atomic_write_json(OUTBOX_FAILED_PATH, failed)
        except Exception:
            pass

    def _normalize_prompt_payload(
        self, text: str, images: Optional[list[dict]] = None
    ) -> tuple[str, list[dict[str, str]], Optional[str]]:
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
                return text, clean_images, "images are too large (8 MB total maximum)"
            clean_images.append({"mime": mime, "data": data, "name": name[:120]})
        if not text and not clean_images:
            return text, clean_images, "empty prompt"
        return text, clean_images, None

    async def submit_prompt(
        self,
        text: str,
        images: Optional[list[dict]] = None,
        tab_id: str | None = None,
        tab_title: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        text, clean_images, err = self._normalize_prompt_payload(text, images)
        if err:
            return {"ok": False, "error": err}
        item = {
            "id": str(request_id or "").strip()[:80] or uuid.uuid4().hex[:12],
            "text": text,
            "images": clean_images,
            "tab_id": str(tab_id or self._selected_tab_id or ""),
            "tab_title": str(tab_title or self._selected_tab_title or ""),
            "created_at": time.time(),
            "attempts": 0,
        }
        for existing in self._outbox:
            if str(existing.get("id") or "") == item["id"]:
                return {
                    "ok": True,
                    "queued": True,
                    "id": str(existing.get("id") or ""),
                    "pending": len(self._outbox),
                    "duplicate": True,
                }
        self._outbox.append(item)
        if not self._save_outbox():
            # Keep in-memory queue so drain can still try, but tell the client
            # durability failed so it does not assume disk persistence.
            self._kick_outbox_drain()
            return {
                "ok": True,
                "queued": True,
                "id": item["id"],
                "pending": len(self._outbox),
                "persist_error": "could not persist outbox to disk",
            }
        self._kick_outbox_drain()
        return {
            "ok": True,
            "queued": True,
            "id": item["id"],
            "pending": len(self._outbox),
        }

    async def _drain_outbox(self) -> None:
        if self._outbox_draining or not self._outbox:
            return
        self._outbox_draining = True
        self._outbox_drain_started_at = time.time()
        try:
            while self._outbox:
                item = self._outbox[0]
                next_at = float(item.get("next_attempt_at") or 0)
                if next_at > time.time():
                    if len(self._outbox) > 1:
                        self._outbox.append(self._outbox.pop(0))
                        await asyncio.sleep(0.05)
                        continue
                    await asyncio.sleep(min(2.0, next_at - time.time()))
                    continue
                tab_id = str(item.get("tab_id") or "")
                tab_title = str(item.get("tab_title") or "")
                if not tab_title and tab_id:
                    tab_title = self._tab_title_for_id(tab_id)
                    item["tab_title"] = tab_title
                text = str(item.get("text") or "")
                try:
                    result = await asyncio.wait_for(
                        self._prompt_locked(
                            text,
                            item.get("images")
                            if isinstance(item.get("images"), list)
                            else [],
                            tab_id,
                            tab_title,
                        ),
                        timeout=50.0,
                    )
                    if result.get("uncertain"):
                        # Enter was dispatched, so an automatic retry could
                        # duplicate a message that Cursor accepted but did not
                        # expose quickly enough. Archive it for explicit retry.
                        err = result.get("error") or "uncertain send outcome"
                        self._archive_failed_outbox_item(item, str(err))
                        self._outbox.pop(0)
                        self._save_outbox()
                        await self._broadcast(
                            {
                                "type": "prompt_failed",
                                "id": str(item.get("id") or ""),
                                "text": text,
                                "tab_id": tab_id,
                                "tab_title": tab_title,
                                "error": f"{err} — tap Retry if it did not appear in Cursor",
                                "attempts": int(item.get("attempts") or 0) + 1,
                                "will_retry": False,
                                "permanent": True,
                            }
                        )
                        continue
                except asyncio.TimeoutError:
                    result = {"ok": False, "error": "send timed out — Cursor may be busy"}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}

                if result.get("ok"):
                    sent_id = str(item.get("id") or "")
                    self._outbox.pop(0)
                    self._save_outbox()
                    await self._broadcast(
                        {
                            "type": "prompt_sent",
                            "id": sent_id,
                            "text": text,
                            "tab_id": tab_id,
                            "tab_title": tab_title,
                            "sent": True,
                        }
                    )
                    continue

                err = str(result.get("error") or "send failed")
                item["attempts"] = int(item.get("attempts") or 0) + 1
                item["last_error"] = err
                item["next_attempt_at"] = time.time() + min(12.0, 1.2 * item["attempts"])
                will_retry = item["attempts"] < OUTBOX_MAX_ATTEMPTS
                self._save_outbox()
                await self._broadcast(
                    {
                        "type": "prompt_failed",
                        "id": str(item.get("id") or ""),
                        "text": text,
                        "tab_id": tab_id,
                        "tab_title": tab_title,
                        "error": err,
                        "attempts": item["attempts"],
                        "will_retry": will_retry,
                        "permanent": not will_retry,
                    }
                )
                if not will_retry:
                    self._archive_failed_outbox_item(item, err)
                    self._outbox.pop(0)
                    self._save_outbox()
                    await self._broadcast(
                        {
                            "type": "prompt_failed",
                            "id": str(item.get("id") or ""),
                            "text": text,
                            "tab_id": tab_id,
                            "tab_title": tab_title,
                            "error": f"{err} (gave up after {OUTBOX_MAX_ATTEMPTS} tries — tap Retry on phone)",
                            "attempts": item["attempts"],
                            "will_retry": False,
                            "permanent": True,
                        }
                    )
                    continue
                # Rotate failed head so one stuck chat cannot block every later prompt.
                if len(self._outbox) > 1:
                    self._outbox.append(self._outbox.pop(0))
                    self._save_outbox()
                    await asyncio.sleep(0.35)
                else:
                    await asyncio.sleep(min(12.0, 1.2 * item["attempts"]))
        finally:
            self._outbox_draining = False
            self._outbox_drain_started_at = 0.0
            if self._outbox:
                self._kick_outbox_drain()

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
                "subagents": _empty_subagents(),
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
                "subagents": _empty_subagents(),
                "approvals": [],
                "rejects": [],
                "updatedAt": time.time(),
            }
            return False
        return True

    async def _ensure_selected_tab_visible(self) -> None:
        if self._resyncing_tab or not self._selected_tab_id or not self.client.connected:
            return
        now = time.time()
        if now - self._last_tab_resync < 4.0:
            return
        tab_id = json.dumps(self._selected_tab_id)
        check = await self.client.evaluate(
            f"""(() => {{
  const id = {tab_id};
  let el = document.getElementById(id);
  if (!el && id.startsWith('index:')) {{
    const index = Number(id.slice(6));
    el = document.querySelectorAll('.glass-sidebar-agent-menu-btn')[index] || null;
  }}
  if (!el) return {{ ok: false }};
  const cls = (el.className || '').toString();
  const active =
    el.getAttribute('aria-selected') === 'true' ||
    /selected|active|aria-selected=\\"true\\"|data-state=\\"active\\"/i.test(cls) ||
    !!el.closest('[aria-selected="true"], [data-state="active"], .selected');
  return {{ ok: true, active }};
}})()"""
        )
        if check and check.get("ok") and not check.get("active"):
            self._last_tab_resync = now
            await self.select_tab(self._selected_tab_id)

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
                "subagents": _empty_subagents(),
                "approvals": [],
                "rejects": [],
                "updatedAt": time.time(),
            }
            await self._broadcast({"type": "state", "state": self.state})
            return self.state

        if not isinstance(extracted, dict):
            extracted = {"ok": False, "error": "empty extract"}

        switching = time.time() < self._tab_switch_deadline
        # Cursor's sidebar exposes no active/selected marker, so CDP cannot tell
        # which chat is open. Track intent instead and project it onto the tabs.
        extracted_active = next(
            (t for t in (extracted.get("tabs") or []) if t.get("active")),
            None,
        )
        if extracted_active and not switching:
            self._selected_tab_id = str(
                extracted_active.get("id") or self._selected_tab_id
            )
            self._selected_tab_title = str(
                extracted_active.get("title") or self._selected_tab_title or ""
            )
        elif self._selected_tab_id or self._selected_tab_title:
            for tab in extracted.get("tabs") or []:
                tab["active"] = (
                    str(tab.get("id") or "") == self._selected_tab_id
                    or self._titles_match(
                        self._selected_tab_title, str(tab.get("title") or "")
                    )
                )
            for group in extracted.get("repos") or []:
                for chat in group.get("chats") or []:
                    chat["active"] = (
                        str(chat.get("id") or "") == self._selected_tab_id
                        or self._titles_match(
                            self._selected_tab_title, str(chat.get("title") or "")
                        )
                    )

        self._stamp_subagent_scope(extracted)
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
            "switching": switching,
            "selectedTabId": self._selected_tab_id,
            "selectedTabTitle": self._selected_tab_title,
        }
        enrich_state_images(self.state)
        self._update_conv_cache()
        fp = json.dumps(
            {
                "m": [
                    (
                        m.get("id"),
                        m.get("text", "")[:80],
                        len(m.get("images") or []),
                        [str(i.get("path") or i.get("src") or "")[:120] for i in (m.get("images") or [])[:6]],
                    )
                    for m in self.state.get("messages") or []
                ],
                "a": self.state.get("approvals"),
                "t": [
                    (t.get("id"), t.get("title"), t.get("active"), t.get("working"))
                    for t in self.state.get("tabs") or []
                ],
                "q": self.state.get("queue"),
                "act": [(x.get("type"), (x.get("text") or "")[:60]) for x in (self.state.get("activity") or [])[-12:]],
                "sub": self.state.get("subagents"),
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
                    "subagents": _empty_subagents(),
                    "approvals": [],
                    "rejects": [],
                    "updatedAt": time.time(),
                }
                await self._broadcast({"type": "state", "state": self.state})
            await asyncio.sleep(max(0.2, POLL_MS / 1000.0))
            self._kick_outbox_drain()
            if BG_CACHE_REFRESH_ENABLED and not self._outbox:
                try:
                    await self._maybe_background_cache_refresh()
                except Exception:
                    pass

    async def prompt(
        self,
        text: str,
        images: Optional[list[dict]] = None,
        tab_id: str = "",
        tab_title: str = "",
    ) -> dict:
        tab_id = str(tab_id or self._selected_tab_id or "")
        tab_title = str(tab_title or self._selected_tab_title or "")
        if not tab_title and tab_id:
            tab_title = self._tab_title_for_id(tab_id)
        direct_error: Optional[str] = None
        try:
            # Timeout wraps the whole locked transaction. Do NOT wait_for()
            # individual lock-holding calls — wait_for runs a child task and
            # deadlocks even a reentrant lock owned by the parent.
            result = await asyncio.wait_for(
                self._prompt_locked(text, images, tab_id, tab_title),
                timeout=50.0,
            )
            if result.get("ok"):
                result["sent"] = True
                result["tab_id"] = tab_id
                result["tab_title"] = tab_title
                return result
            direct_error = str(result.get("error") or "send failed")
        except asyncio.TimeoutError:
            direct_error = "send timed out — queued for retry"
        except Exception as exc:
            direct_error = str(exc)
        # After Enter, "not found" is ambiguous — auto-retry can duplicate.
        if direct_error and "message not found in conversation after send" in direct_error:
            return {
                "ok": False,
                "error": direct_error + " — tap Retry if it did not appear in Cursor",
                "tab_id": tab_id,
                "tab_title": tab_title,
                "uncertain": True,
            }
        queued = await self.submit_prompt(text, images, tab_id, tab_title)
        if direct_error:
            queued["direct_error"] = direct_error
        return queued

    async def _prompt_locked(
        self,
        text: str,
        images: Optional[list[dict]],
        tab_id: str,
        tab_title: str,
    ) -> dict:
        async with self._cmd_lock():
            ready = await self._ensure_conversation_for_send(tab_id, tab_title)
            if not ready.get("ok"):
                return {
                    "ok": False,
                    "error": ready.get("error")
                    or f"could not switch to conversation {tab_title or tab_id}",
                }
            return await self._send_prompt_impl(text, images)

    async def _read_active_sidebar_tab(self) -> dict:
        if not await self.ensure_connected():
            return {"ok": False}
        result = await self.client.evaluate(
            """(() => {
  const buttons = [...document.querySelectorAll('.glass-sidebar-agent-menu-btn')];
  for (const el of buttons) {
    const cls = (el.className || '').toString();
    const active =
      el.getAttribute('aria-selected') === 'true' ||
      /selected|active|data-state="active"/i.test(cls) ||
      !!el.closest('[aria-selected="true"], [data-state="active"]');
    if (!active) continue;
    const rawTitle = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    const title = rawTitle.replace(/\\s+(\\d+\\s*[smhd]|just now|now)$/i, '').trim() || rawTitle;
    return { ok: true, id: el.id || '', title };
  }
  return { ok: false };
})()"""
        )
        return result if isinstance(result, dict) else {"ok": False}

    @staticmethod
    def _normalize_title(title: str) -> str:
        title = (title or "").replace("\n", " ").strip()
        title = " ".join(title.split())
        import re

        return re.sub(
            r"\s+(\d+\s*[smhdw]\s+ago|\d+\s+[smhdw]\s+ago|just now|now)$",
            "",
            title,
            flags=re.I,
        ).strip()

    @staticmethod
    def _titles_match(want: str, got: str) -> bool:
        want = AgentHub._normalize_title(want).lower()
        got = AgentHub._normalize_title(got).lower()
        if not want or not got:
            return False
        return want == got or got.startswith(want) or want.startswith(got)

    def _message_text_matches(self, want: str, got: str) -> bool:
        want_n = self._normalize_title(want).lower()
        got_n = self._normalize_title(got).lower()
        if not want_n or not got_n:
            return False
        if want_n == got_n or got_n.startswith(want_n) or want_n.startswith(got_n):
            return True
        short = want_n[: min(120, len(want_n))]
        return short in got_n or got_n[:120] in want_n

    def _count_matching_human_prompts(self, text: str) -> int:
        snippet = self._normalize_title(text)
        if not snippet:
            return 0
        count = 0
        for message in self.state.get("messages") or []:
            kind = str(message.get("type") or message.get("role") or "").lower()
            if kind != "human":
                continue
            if self._message_text_matches(snippet, str(message.get("text") or "")):
                count += 1
        return count

    def _prompt_in_queue(self, text: str) -> bool:
        """Cursor parks prompts in a queue while the agent is busy.

        Those never reach the transcript until the run finishes, so treating a
        queued prompt as "not sent" caused endless resends.
        """
        snippet = self._normalize_title(text)
        if not snippet:
            return False
        for item in self.state.get("queue") or []:
            if self._message_text_matches(snippet, str(item.get("text") or "")):
                return True
        return False

    def _verify_prompt_in_transcript(self, text: str, baseline: int = 0) -> bool:
        snippet = self._normalize_title(text)
        if not snippet:
            return True
        # Require a NEW matching human message, not a historical duplicate.
        if self._count_matching_human_prompts(text) > max(0, int(baseline or 0)):
            return True
        return self._prompt_in_queue(text)

    async def _await_prompt_in_transcript(
        self,
        text: str,
        baseline: int = 0,
        attempts: int = 5,
        delay: float = 0.6,
    ) -> bool:
        """Cursor renders the sent message asynchronously, so poll briefly.

        A single check races the UI and reports a delivered message as failed,
        which then gets queued and sent a second time. Baseline prevents older
        identical prompts (e.g. "continue") from counting as success.
        """
        for index in range(attempts):
            if self._verify_prompt_in_transcript(text, baseline=baseline):
                return True
            if index == attempts - 1:
                break
            await asyncio.sleep(delay)
            try:
                await self.refresh_state()
            except Exception:
                pass
        return False

    async def _maybe_background_cache_refresh(self) -> None:
        if (
            self._bg_cache_running
            or self._outbox
            or self._resyncing_tab
            or self._outbox_draining
            or not self.client.connected
        ):
            return
        if time.time() - self._last_bg_cache_at < BG_CACHE_MIN_INTERVAL_S:
            return
        if self.state.get("loading") or time.time() < self._tab_switch_deadline:
            return
        tabs = self.state.get("tabs") or []
        if len(tabs) < 2:
            return
        current = str(self._selected_tab_id or "")
        stalest: Optional[dict[str, Any]] = None
        stalest_at = time.time()
        for tab in tabs:
            tid = str(tab.get("id") or "")
            if not tid or tid == current:
                continue
            entry = self._conv_cache.get(tid)
            updated = float(entry.get("updatedAt") or 0) if entry else 0.0
            if updated < stalest_at:
                stalest_at = updated
                stalest = tab
        if not stalest:
            return
        self._bg_cache_running = True
        self._last_bg_cache_at = time.time()
        restore_id = current
        restore_title = self._selected_tab_title
        stale_id = str(stalest.get("id") or "")
        stale_title = str(stalest.get("title") or "")
        try:
            async with self._cmd_lock():
                switched = await self._select_tab_impl(stale_id, stale_title)
                if switched.get("ok"):
                    cached = self.conversation_cache(stale_id)
                    if cached.get("messages"):
                        await self._broadcast(
                            {
                                "type": "cache_update",
                                "tabId": stale_id,
                                "cache": cached,
                            }
                        )
                if restore_id and restore_id != stale_id:
                    await self._select_tab_impl(restore_id, restore_title)
        finally:
            self._bg_cache_running = False

    async def _ensure_conversation_for_send(self, tab_id: str, tab_title: str) -> dict:
        """Caller must already hold `_cmd_lock`.

        Cursor's sidebar has no active-state marker, so `_read_active_sidebar_tab`
        usually cannot answer. Fall back to our own tracked selection.
        """
        tab_id = str(tab_id or "").strip()
        tab_title = str(tab_title or "").strip()
        if not tab_id and not tab_title:
            return {"ok": True}

        def already_there() -> bool:
            if tab_id and str(self._selected_tab_id or "") == tab_id:
                return True
            if (
                not tab_id
                and tab_title
                and self._titles_match(tab_title, self._selected_tab_title)
            ):
                return True
            return False

        active = await self._read_active_sidebar_tab()
        if active.get("ok"):
            active_id = str(active.get("id") or "")
            active_title = str(active.get("title") or "")
            if tab_id and active_id == tab_id:
                return {"ok": True}
            if not (tab_id and active_id and active_id != tab_id):
                if tab_title and self._titles_match(tab_title, active_title):
                    return {"ok": True}
        elif already_there():
            return {"ok": True}

        if tab_id:
            switched = await self._select_tab_body(tab_id, load_history=False)
            if switched.get("ok"):
                return {"ok": True}
        if tab_title:
            switched = await self._select_tab_by_title_impl(tab_title)
            if switched.get("ok"):
                return {"ok": True}
        if already_there():
            return {"ok": True}
        label = tab_title or tab_id
        return {"ok": False, "error": f"could not switch to conversation {label}"}

    async def _select_tab_by_title_impl(self, title: str) -> dict:
        if not await self.ensure_connected():
            return {"ok": False, "error": self.state.get("error") or "no cdp"}
        want = str(title or "").strip()
        if not want:
            return {"ok": False, "error": "missing conversation title"}
        point = await self.client.evaluate(
            f"""(() => {{
  const want = {json.dumps(want)}.toLowerCase();
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const stripAge = (s) => norm(s).replace(/\\s+(\\d+\\s*[smhd]|just now|now)$/i, '').trim();
  for (const el of document.querySelectorAll('.glass-sidebar-agent-menu-btn')) {{
    const raw = norm(el.innerText || el.textContent || '');
    const title = stripAge(raw);
    if (!title) continue;
    if (title === want || title.startsWith(want) || want.startsWith(title)) {{
      const id = el.id || '';
      el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
      const r = el.getBoundingClientRect();
      const x = r.left + Math.min(r.width * .35, 120);
      const y = r.top + r.height / 2;
      return {{ ok: true, id, title, x, y }};
    }}
  }}
  return {{ ok: false, error: 'conversation title not found' }};
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
        self._selected_tab_id = str(point.get("id") or "")
        self._selected_tab_title = str(point.get("title") or want)
        self._tab_switch_deadline = time.time() + 4.0
        await asyncio.sleep(0.35)
        self._tab_switch_deadline = 0.0
        await self.refresh_state()
        return {"ok": True, "title": self._selected_tab_title, "id": self._selected_tab_id}

    @staticmethod
    def _split_slash_command(text: str) -> tuple[str, str]:
        """Separate a leading Cursor slash command from the message body."""
        import re

        match = re.match(
            r"^\s*(/(?:plan|ask|debug|multitask|agent|edit|triage|model))\b[ \t]*(.*)$",
            str(text or ""),
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return "", str(text or "")
        return match.group(1), match.group(2).strip()

    async def _mode_chip_present(self) -> bool:
        found = await self.client.evaluate(
            """(() => {
  for (const el of document.querySelectorAll('[class*="chip"], [class*="badge"], [class*="mode"]')) {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t && t.length < 40 && /^(multitask|plan|ask|debug|edit|triage)$/i.test(t)) return true;
  }
  return false;
})()"""
        )
        return bool(found)

    async def _clear_composer(self) -> bool:
        """Empty the composer including any leftover mode chip.

        An accepted slash command becomes a chip that survives select-all +
        delete, so it would silently apply to every later message.
        """
        await self.client.press_key("a", "KeyA", 65, modifiers=2)
        await asyncio.sleep(0.05)
        await self.client.press_key("Backspace", "Backspace", 8)
        await asyncio.sleep(0.08)
        for _ in range(4):
            if not await self._mode_chip_present():
                return True
            await self.client.press_key("Backspace", "Backspace", 8)
            await asyncio.sleep(0.2)
        return not await self._mode_chip_present()

    async def _menu_is_open(self) -> bool:
        found = await self.client.evaluate(
            """(() => {
  for (const el of document.querySelectorAll('[role="menu"]')) {
    const r = el.getBoundingClientRect();
    if (r.width > 40 && r.height > 20) return true;
  }
  return false;
})()"""
        )
        return bool(found)

    async def _dismiss_open_menu(self) -> None:
        """Close a lingering command menu so Enter reaches the composer."""
        for _ in range(2):
            if not await self._menu_is_open():
                return
            await self.client.press_key("Escape", "Escape", 27)
            await asyncio.sleep(0.2)

    async def _accept_slash_command(self, command: str) -> bool:
        """Insert a slash command and accept it into a mode chip via Tab."""
        await self.client.insert_text(command)
        await asyncio.sleep(0.45)
        if not await self._menu_is_open():
            # No menu appeared; fall back to sending the text literally.
            await self._clear_composer()
            return False
        await self.client.press_key("Tab", "Tab", 9)
        await asyncio.sleep(0.4)
        if await self._menu_is_open():
            await self._dismiss_open_menu()
            await self._clear_composer()
            return False
        return True

    async def _send_prompt_impl(self, text: str, images: Optional[list[dict]] = None) -> dict:
        text, clean_images, err = self._normalize_prompt_payload(text, images)
        if err:
            return {"ok": False, "error": err}
        if not await self.ensure_connected():
            return {"ok": False, "error": self.state.get("error") or "no cdp"}
        slash_cmd, body_text = self._split_slash_command(text)
        # Verify against the body: accepting a slash command turns it into a
        # mode chip, so it never appears in the transcript text.
        verify_text = body_text or text
        baseline = self._count_matching_human_prompts(verify_text) if verify_text else 0
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
        await self._clear_composer()
        if slash_cmd:
            # Typing a slash command opens Cursor's command menu, which swallows
            # the Enter that should send. Accept it with Tab so it becomes a mode
            # chip, then type the real message.
            accepted = await self._accept_slash_command(slash_cmd)
            if not accepted:
                body_text = text
        if body_text:
            await self.client.insert_text(body_text)
        await self._dismiss_open_menu()
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
    return {{ ok: true, count: files.length, via: 'file-input' }};
  }}
  // Cursor treats each paste as a separate composer operation. Put every file
  // in one clipboard payload so N images remain one prompt.
  const transfer = new DataTransfer();
  files.forEach((file) => transfer.items.add(file));
  const event = new ClipboardEvent('paste', {{
    bubbles: true,
    cancelable: true,
    clipboardData: transfer,
  }});
  input.dispatchEvent(event);
  return {{ ok: true, count: files.length, via: 'paste' }};
}})()""",
                timeout=20.0,
            )
            if not attached or not attached.get("ok"):
                return {
                    "ok": False,
                    "error": (attached or {}).get("error") or "could not attach image",
                }
            # File processing is asynchronous in Cursor. Sending while the
            # attachment placeholders are still being created can split text
            # from images, so wait until the composer reflects every file.
            expected = len(clean_images)
            ready = False
            for _ in range(24):
                ready = bool(
                    await self.client.evaluate(
                        f"""(() => {{
  const expected = {expected};
  const input = document.querySelector(
    "#workbench\\\\.parts\\\\.auxiliarybar [contenteditable='true'], " +
    "#workbench\\\\.parts\\\\.auxiliarybar textarea, " +
    ".composer-bar [contenteditable='true'], [contenteditable='true'], textarea"
  );
  if (!input) return false;
  let root = input;
  for (let i = 0; root.parentElement && i < 6; i++) root = root.parentElement;
  const fileInput = [...root.querySelectorAll('input[type="file"]')].find(
    (el) => !el.disabled && (!el.accept || /image|\\*/i.test(el.accept))
  );
  if (fileInput && fileInput.files && fileInput.files.length >= expected) return true;
  const previews = root.querySelectorAll(
    'img[src^="blob:"], img[src^="data:image/"], [class*="attachment"], [data-file-name]'
  );
  return previews.length >= expected;
}})()"""
                    )
                )
                if ready:
                    break
                await asyncio.sleep(0.15)
            if not ready:
                return {
                    "ok": False,
                    "error": "Cursor did not finish attaching all images",
                }
        await asyncio.sleep(0.08)
        await self.client.press_key("Enter", "Enter", 13)
        # Never follow Enter with Ctrl+Enter. If attachment processing delayed
        # the first submit, a second shortcut can create a separate prompt.
        await asyncio.sleep(0.35)
        await self.refresh_state()
        if verify_text and not await self._await_prompt_in_transcript(
            verify_text, baseline=baseline
        ):
            return {
                "ok": False,
                "error": "message not found in conversation after send",
                "uncertain": True,
            }
        self.usage_stats["prompts"] = int(self.usage_stats.get("prompts") or 0) + 1
        model_name = str(self.state.get("model") or "")
        if model_name:
            by = self.usage_stats["byModel"]
            by[model_name] = int(by.get(model_name) or 0) + 1
        return {"ok": True, "text": text, "sent": True}

    async def fetch_local_image(self, path: str) -> dict:
        import base64
        from pathlib import Path

        path = normalize_path(path)
        allowed = allowed_image_paths(self.state)
        if not path or path not in allowed:
            return {"ok": False, "error": "image is not in the current chat"}
        resolved, err = file_browser.file_response_path(path)
        if resolved is None:
            # Chat-referenced paths may sit outside default browse roots (e.g. C:\FXShots).
            candidate = Path(path)
            if not candidate.is_file():
                return {"ok": False, "error": err or "file not found"}
            resolved = candidate.resolve()
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
        async with self._cmd_lock():
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
        async with self._cmd_lock():
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
        async with self._cmd_lock():
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

    @staticmethod
    def _mode_slash(mode: str) -> str:
        mode = (mode or "").strip().lower()
        aliases = {
            "agent": "",
            "plan": "/plan",
            "ask": "/ask",
            "debug": "/debug",
            "multitask": "/multitask",
            "triage": "/multitask",
            "edit": "/edit",
        }
        return aliases.get(mode, f"/{mode}" if mode else "")

    async def open_workspace(self, workspace_path: str) -> dict:
        async with self._cmd_lock():
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            return await self._open_workspace_impl(workspace_path)

    async def _open_workspace_impl(self, workspace_path: str) -> dict:
        from pathlib import Path

        raw = str(workspace_path or "").strip().strip('"')
        if not raw:
            return {"ok": False, "error": "empty workspace path"}
        folder = Path(raw)
        if not folder.is_dir():
            return {"ok": False, "error": f"folder not found: {raw}"}
        resolved = str(folder.resolve())
        base = folder.name
        clicked = await self.client.evaluate(
            f"""(async () => {{
  const base = {json.dumps(base)};
  const full = {json.dumps(resolved)};
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const match = (t) => {{
    const s = norm(t);
    if (!s) return false;
    if (s === base) return true;
    if (s.toLowerCase() === base.toLowerCase()) return true;
    if (s.startsWith(base + ' ')) return true;
    if (full && s.includes(base)) return true;
    return false;
  }};
  const selectors = [
    '.glass-sidebar-agent-menu-btn',
    '[class*="sidebar"] button',
    '[role="treeitem"]',
    '[role="button"]',
    'a',
  ];
  for (const sel of selectors) {{
    for (const el of document.querySelectorAll(sel)) {{
      const t = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
      if (!match(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) continue;
      el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
      (el.closest('button,a,[role=treeitem]') || el).click();
      await new Promise((resolve) => setTimeout(resolve, 450));
      return {{ ok: true, via: 'sidebar', name: base }};
    }}
  }}
  return {{ ok: false, needHost: true, name: base }};
}})()""",
            timeout=12.0,
        )
        if isinstance(clicked, dict) and clicked.get("ok"):
            await asyncio.sleep(0.35)
            await self.refresh_state()
            return clicked
        host = await self._open_workspace_via_cursor_cli(resolved)
        if host.get("ok"):
            await asyncio.sleep(1.0)
            await self.ensure_connected()
            await self.refresh_state()
            return {**host, "warning": "opened via Cursor CLI — may take a moment"}
        return {
            "ok": False,
            "error": f'could not switch workspace to "{base}" — open the folder in Cursor first',
        }

    async def _open_workspace_via_cursor_cli(self, folder: str) -> dict:
        cursor = os.path.expandvars(r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe")
        if not os.path.isfile(cursor):
            return {"ok": False, "error": "cursor.exe not found"}
        try:
            proc = await asyncio.create_subprocess_exec(
                cursor,
                folder,
                "--reuse-window",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=8.0)
            return {"ok": True, "via": "cursor-cli", "path": folder}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def new_agent_setup(
        self,
        workspace: str = "",
        model: str = "",
        mode: str = "",
    ) -> dict:
        warnings: list[str] = []
        async with self._cmd_lock():
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            if workspace.strip():
                opened = await self._open_workspace_impl(workspace.strip())
                if not opened.get("ok"):
                    return opened
                if opened.get("warning"):
                    warnings.append(str(opened["warning"]))
            created = await self._new_chat_impl()
            if not created.get("ok"):
                return created
            await asyncio.sleep(0.35)
            if model.strip():
                model_result = await self._set_model_impl(model.strip())
                if not model_result.get("ok"):
                    warnings.append(
                        model_result.get("error") or f'model "{model}" not set'
                    )
            await self.refresh_state()
            out: dict[str, Any] = {
                "ok": True,
                "via": created.get("via"),
                "workspace": workspace.strip() or self.state.get("workspace"),
                "model": self.state.get("model") or model.strip(),
                "modeSlash": self._mode_slash(mode),
            }
            if warnings:
                out["warnings"] = warnings
            return out

    async def new_chat(self) -> dict:
        async with self._cmd_lock():
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            return await self._new_chat_impl()

    async def _new_chat_impl(self) -> dict:
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
                    # Clear prior target so the first prompt cannot bounce back.
                    self._selected_tab_id = ""
                    self._selected_tab_title = ""
                    self._tab_switch_deadline = time.time() + 2.0
                    await self.refresh_state()
                    return {"ok": True, "via": sel}
            except Exception:
                continue
        if await self.client.click_by_label(["New Agent", "New Chat", "New chat"]):
            await asyncio.sleep(0.35)
            self._selected_tab_id = ""
            self._selected_tab_title = ""
            self._tab_switch_deadline = time.time() + 2.0
            await self.refresh_state()
            return {"ok": True, "via": "label"}
        return {"ok": False, "error": "New Agent control not found — open Agents window in Cursor"}

    async def select_tab(self, tab_id: str, tab_title: str = "") -> dict:
        self._resyncing_tab = True
        try:
            return await self._select_tab_impl(tab_id, tab_title)
        finally:
            self._resyncing_tab = False

    async def _select_tab_impl(
        self, tab_id: str, tab_title: str = "", load_history: bool = True
    ) -> dict:
        async with self._cmd_lock():
            return await self._select_tab_body(tab_id, tab_title, load_history)

    async def _select_tab_body(
        self, tab_id: str, tab_title: str = "", load_history: bool = True
    ) -> dict:
        """Tab switch body. Caller must already hold `_cmd_lock`."""
        if not await self.ensure_connected():
            return {"ok": False, "error": self.state.get("error") or "no cdp"}
        tab_id = str(tab_id or "")
        if not tab_id:
            return {"ok": False, "error": "missing conversation id"}
        before_messages = json.dumps(
            [
                (m.get("id"), m.get("type"), str(m.get("text") or "")[:240])
                for m in (self.state.get("messages") or [])
            ],
            sort_keys=True,
        )
        point = await self.client.evaluate(
            f"""(() => {{
  const id = {json.dumps(tab_id)};
  const wantTitle = {json.dumps(str(tab_title or ""))};
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const stripAge = (s) => norm(s).replace(/\\s+(\\d+\\s*[smhd]|just now|now)$/i, '').trim();
  let el = document.getElementById(id);
  if (el && !el.classList.contains('glass-sidebar-agent-menu-btn')) el = null;
  if (!el && id.startsWith('index:')) {{
    const index = Number(id.slice(6));
    el = document.querySelectorAll('.glass-sidebar-agent-menu-btn')[index] || null;
  }}
  // Cursor regenerates React row ids whenever the sidebar rerenders. A phone
  // can therefore hold a valid conversation id that no longer exists after
  // switching away. Resolve the same row by its title instead.
  if (!el && wantTitle) {{
    const want = stripAge(wantTitle);
    el = [...document.querySelectorAll('.glass-sidebar-agent-menu-btn')].find((row) => {{
      const got = stripAge(row.innerText || row.textContent || '');
      return got && (got === want || got.startsWith(want) || want.startsWith(got));
    }}) || null;
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
  return {{ ok: true, id: el.id || id, title, x, y }};
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
        self._selected_tab_id = str(point.get("id") or tab_id)
        self._selected_tab_title = str(
            point.get("title") or tab_title or self._selected_tab_title or ""
        )
        self._tab_switch_deadline = time.time() + 4.0
        if load_history:
            await self.client.evaluate(
                """(async () => {
  const hosts = document.querySelectorAll(
    '.composer-messages-container, .agent-transcript-scroll, [class*="agent-transcript"]'
  );
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (const el of hosts) {
    if (el.scrollHeight <= el.clientHeight + 40) continue;
    el.scrollTop = el.scrollHeight;
    el.dispatchEvent(new Event('scroll', { bubbles: true }));
    await sleep(80);
    for (let step = 0; step < 5; step++) {
      const max = Math.max(0, el.scrollHeight - el.clientHeight);
      el.scrollTop = Math.max(0, max - step * Math.max(300, max / 5));
      el.dispatchEvent(new Event('scroll', { bubbles: true }));
      await sleep(45);
    }
    // Leave Cursor pinned to the newest message, not scrolled to the top.
    el.scrollTop = el.scrollHeight;
    el.dispatchEvent(new Event('scroll', { bubbles: true }));
  }
  return true;
})()""",
                timeout=12.0,
            )
            await asyncio.sleep(0.35)
        else:
            await asyncio.sleep(0.2)
        # Cursor's sidebar has no reliable active marker, so only treat the
        # switch as failed when we positively read a DIFFERENT chat as active.
        mismatch = ""
        for _ in range(4):
            active = await self._read_active_sidebar_tab()
            if not active.get("ok"):
                mismatch = ""
                break
            active_id = str(active.get("id") or "")
            active_title = str(active.get("title") or "")
            if (tab_id and active_id == tab_id) or self._titles_match(
                self._selected_tab_title, active_title
            ):
                self._selected_tab_id = active_id or tab_id
                self._selected_tab_title = active_title or self._selected_tab_title
                mismatch = ""
                break
            mismatch = active_title or active_id
            await asyncio.sleep(0.15)
        if mismatch:
            self._tab_switch_deadline = 0.0
            return {
                "ok": False,
                "error": f"click landed on a different chat ({mismatch})",
            }
        # Cursor swaps transcript DOM asynchronously after the row click. Wait
        # for it to differ from the previous chat before assigning it to the
        # selected tab's cache. There is no usable active-row marker.
        for _ in range(12):
            await asyncio.sleep(0.2)
            await self.refresh_state()
            current_messages = json.dumps(
                [
                    (m.get("id"), m.get("type"), str(m.get("text") or "")[:240])
                    for m in (self.state.get("messages") or [])
                ],
                sort_keys=True,
            )
            if current_messages != before_messages:
                break
        self._tab_switch_deadline = 0.0
        await self.refresh_state()
        cached = self.conversation_cache(self._selected_tab_id or tab_id)
        return {
            "ok": True,
            "title": self._selected_tab_title or point.get("title") or "",
            "cache": cached,
        }

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
        async with self._cmd_lock():
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
        async with self._cmd_lock():
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
        async with self._cmd_lock():
            if not await self.ensure_connected():
                return {"ok": False, "error": self.state.get("error") or "no cdp"}
            return await self._set_model_impl(model_label)

    async def _set_model_impl(self, model_label: str) -> dict:
        label = (model_label or "").strip()
        if not label:
            return {"ok": False, "error": "empty model"}
        if label.startswith("label::"):
            label = label[7:]
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
        async with self._cmd_lock():
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
        async with self._cmd_lock():
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
        async with self._cmd_lock():
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
