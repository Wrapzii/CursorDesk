"""Resolve agent message image paths on disk for phone embedding."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import file_browser

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tga", ".tif", ".tiff"}
IMAGE_FILE_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s<>\"'`|\r\n]+?\.(?:png|jpe?g|webp|gif|bmp|tiff?))"
    r"|(?:/(?:[^\s<>\"'`|\r\n])+?\.(?:png|jpe?g|webp|gif|bmp|tiff?))"
    r"|(?:\./[^\s<>\"'`|\r\n]+?\.(?:png|jpe?g|webp|gif|bmp|tiff?))",
    re.IGNORECASE,
)
DIR_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s<>\"'`|\r\n*?]+|/(?:[^\s<>\"'`|\r\n*?]+)|\./[^\s<>\"'`|\r\n]+)",
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)", re.IGNORECASE)


def _clean_token(raw: str) -> str:
    s = (raw or "").strip().strip("\"'`.,;:!?)")
    return s.replace("\\)", "").replace("\\(", "")


def normalize_path(raw: str) -> str:
    s = _clean_token(raw)
    if not s:
        return ""
    if s.startswith("file://"):
        s = unquote(s[7:])
    elif s.startswith("local:"):
        s = s[6:]
    elif s.startswith("vscode-file://"):
        parsed = urlparse(s)
        s = unquote(parsed.path or "")
        if os.name == "nt" and s.startswith("/") and len(s) > 2 and s[2] == ":":
            s = s[1:]
    s = os.path.expanduser(os.path.expandvars(s))
    try:
        return str(Path(s).resolve())
    except Exception:
        return s


def _allowed_file(path: str) -> str | None:
    resolved, _ = file_browser.file_response_path(path)
    return str(resolved) if resolved else None


def list_images_in_dir(dir_path: str, limit: int = 12) -> list[str]:
    base = Path(normalize_path(dir_path))
    if not base.is_dir() or not file_browser.list_dir(str(base)).get("ok"):
        return []
    found: list[Path] = []
    try:
        for child in base.iterdir():
            if child.is_file() and child.suffix.lower() in IMAGE_EXT:
                found.append(child)
    except Exception:
        return []
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[str] = []
    for child in found[:limit]:
        allowed = _allowed_file(str(child))
        if allowed:
            out.append(allowed)
    return out


def ingest_path_token(raw: str, seen: set[str], out: list[str]) -> None:
    path = normalize_path(raw)
    if not path:
        return
    p = Path(path)
    if p.is_file() and p.suffix.lower() in IMAGE_EXT:
        allowed = _allowed_file(path)
        if allowed and allowed not in seen:
            seen.add(allowed)
            out.append(allowed)
        return
    if p.is_dir():
        for img in list_images_in_dir(path):
            if img not in seen:
                seen.add(img)
                out.append(img)


def paths_from_text(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for match in IMAGE_FILE_RE.finditer(text):
        ingest_path_token(match.group(0), seen, ordered)
    for match in MARKDOWN_IMAGE_RE.finditer(text):
        ingest_path_token(match.group(1), seen, ordered)
    for match in DIR_RE.finditer(text):
        token = _clean_token(match.group(0))
        if IMAGE_FILE_RE.search(token):
            continue
        ingest_path_token(token, seen, ordered)
    return ordered


def image_record(path: str, alt: str = "", source: str = "path") -> dict[str, Any]:
    name = Path(path).name
    return {
        "id": path,
        "src": f"local:{path}",
        "path": path,
        "alt": alt or name,
        "source": source,
    }


def enrich_message_images(message: dict[str, Any]) -> None:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_path(path: str, alt: str = "", source: str = "path") -> None:
        allowed = _allowed_file(path)
        if not allowed or allowed in seen:
            return
        seen.add(allowed)
        merged.append(image_record(allowed, alt, source))

    for img in message.get("images") or []:
        if not isinstance(img, dict):
            continue
        src = str(img.get("src") or "")
        alt = str(img.get("alt") or "")
        path = str(img.get("path") or "")
        local = _allowed_file(path) if path else None
        if not local:
            for candidate in (src, alt):
                resolved = normalize_path(candidate)
                if resolved and Path(resolved).suffix.lower() in IMAGE_EXT:
                    local = _allowed_file(resolved)
                    if local:
                        break
        if local:
            add_path(local, alt, "dom")
            continue
        if src.startswith("http://") or src.startswith("https://") or src.startswith("data:image/"):
            key = src
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(img))

    for path in paths_from_text(str(message.get("text") or "")):
        add_path(path, source="text")

    message["images"] = merged[:20]


def enrich_state_images(state: dict[str, Any]) -> None:
    for message in state.get("messages") or []:
        if isinstance(message, dict):
            enrich_message_images(message)


def allowed_image_paths(state: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()
    for message in state.get("messages") or []:
        for img in message.get("images") or []:
            path = str(img.get("path") or "")
            if path:
                allowed.add(normalize_path(path))
            src = str(img.get("src") or "")
            if src.startswith("local:"):
                allowed.add(normalize_path(src[6:]))
    return allowed
