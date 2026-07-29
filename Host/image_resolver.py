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
RELATIVE_IMAGE_RE = re.compile(
    r"(?:Saved|Content|Plugins|docs|screenshots)(?:[\\/][\w.$\- ]+?)+\.(?:png|jpe?g|webp|gif|bmp|tiff?)",
    re.IGNORECASE,
)
RELATIVE_DIR_RE = re.compile(
    r"(?:Saved|Content)(?:[\\/][\w.$\- ]+)+",
    re.IGNORECASE,
)
GLOB_DIR_RE = re.compile(
    r"((?:Saved|Content)(?:[\\/][\w.$\- ]+)+)\\\*\.\w+",
    re.IGNORECASE,
)
DIR_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s<>\"'`|\r\n*?]+|/(?:[^\s<>\"'`|\r\n*?]+)|\./[^\s<>\"'`|\r\n]+)",
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)", re.IGNORECASE)
LOOSE_FILE_RE = re.compile(
    r"\b([A-Za-z][\w.\-]*\.(?:png|jpe?g|webp|gif|bmp|tiff?))\b",
    re.IGNORECASE,
)
ABS_DIR_RE = re.compile(
    r"([A-Za-z]:\\[^\s:]+(?:\\[^\s:]+)*)",
    re.IGNORECASE,
)
IMAGE_INTENT_RE = re.compile(
    r"\b(show\s+(me\s+)?(the\s+)?images?|screenshots?|previews?|renders?|thumbnails?)\b",
    re.IGNORECASE,
)
DEFAULT_IMAGE_SUBDIRS = (
    "Saved/FXShots/view",
    "Saved/FXShots",
    "Saved/Screenshots",
    "docs/screenshots",
)
SIZE_TAIL_RE = re.compile(
    r"[\s\u2014\u2013\-]+(?:\d[\d,]*\s*bytes?)$",
    re.IGNORECASE,
)
LISTING_DATE_RE = re.compile(
    r"\.\s*(?:all\s+)?written\s+\d{1,2}/\d{1,2}/\d{2,4}.*$",
    re.IGNORECASE,
)
AGE_TAIL_RE = re.compile(
    r"\s+(?:\d+\s*[smhdw]\s+ago|just\s+now|now)\s*$",
    re.IGNORECASE,
)
DATE_TAIL_RE = re.compile(
    r"\s+\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?.*$",
    re.IGNORECASE,
)


def _clean_token(raw: str) -> str:
    s = (raw or "").strip().strip("\"'`.,;:!?)")
    return s.replace("\\)", "").replace("\\(", "")


def clean_display_token(raw: str) -> str:
    s = _clean_token(raw)
    if not s:
        return ""
    s = SIZE_TAIL_RE.sub("", s).strip()
    s = LISTING_DATE_RE.sub("", s).strip()
    s = AGE_TAIL_RE.sub("", s).strip()
    s = DATE_TAIL_RE.sub("", s).strip()
    return s.strip(" \t\u2014\u2013\-:;,")


def _display_path_score(path: str) -> int:
    low = path.lower().replace("/", "\\")
    score = 0
    if "\\view\\" in low:
        score += 4
    if low.endswith((".jpg", ".jpeg")):
        score += 2
    if "fxshots" in low:
        score += 1
    return score


def prefer_display_paths(paths: list[str]) -> list[str]:
    order: list[str] = []
    best: dict[str, str] = {}
    for path in paths:
        stem = _compact_label(Path(path).stem)
        if stem not in best:
            order.append(stem)
        current = best.get(stem)
        if not current or _display_path_score(path) > _display_path_score(current):
            best[stem] = path
    return [best[stem] for stem in order if stem in best]


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


def project_roots(state: dict[str, Any] | None = None) -> list[Path]:
    seen: set[str] = set()
    roots: list[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
            key = str(resolved)
            if key in seen:
                return
            if resolved.is_dir():
                seen.add(key)
                roots.append(resolved)
        except Exception:
            pass

    env = os.environ.get("CURSORDESK_PROJECT")
    if env:
        add(Path(env))
    add(file_browser.PROJECT)
    ws = str((state or {}).get("workspace") or "").strip()
    for base in (
        Path.home() / "Documents" / "Unreal Projects",
        Path.home() / "Documents",
        Path.home(),
    ):
        if ws:
            add(base / ws)
    return roots


def resolve_token_path(raw: str, roots: list[Path]) -> str:
    token = _clean_token(raw)
    if not token:
        return ""
    if re.match(r"[A-Za-z]:\\", token) or token.startswith("/"):
        return normalize_path(token)
    variants = {token, token.replace("/", "\\"), token.replace("\\", "/")}
    for root in roots:
        for variant in variants:
            try:
                cand = (root / variant).resolve()
                if cand.exists():
                    return str(cand)
            except Exception:
                continue
    return ""


def _compact_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def chat_image_context(chat_texts: list[str], message_text: str = "") -> bool:
    blob = _chat_blob(chat_texts + [message_text]).lower()
    if IMAGE_INTENT_RE.search(blob):
        return True
    if "fxshots" in blob.replace("/", "\\"):
        return True
    if re.search(r"\b(firebolt|icewall|aegis|ember)\b", blob, re.I):
        return True
    return False


def candidate_image_dirs(chat_texts: list[str], roots: list[Path]) -> list[Path]:
    seen: set[str] = set()
    dirs: list[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
            key = str(resolved)
            if key in seen or not resolved.is_dir():
                return
            seen.add(key)
            dirs.append(resolved)
        except Exception:
            pass

    for text in chat_texts:
        for match in ABS_DIR_RE.finditer(text or ""):
            add(Path(normalize_path(match.group(1).rstrip(":\\"))))
        for match in RELATIVE_DIR_RE.finditer(text or ""):
            resolved = resolve_token_path(match.group(0), roots)
            if resolved:
                add(Path(resolved))
    for root in roots:
        for sub in DEFAULT_IMAGE_SUBDIRS:
            add(root / sub)
    if chat_image_context(chat_texts):
        unreal_root = Path.home() / "Documents" / "Unreal Projects"
        if unreal_root.is_dir():
            for project in unreal_root.iterdir():
                if project.is_dir():
                    for sub in ("Saved/FXShots/view", "Saved/FXShots"):
                        add(project / sub)
    return dirs


def find_image_by_label(label: str, dirs: list[Path]) -> str | None:
    stem = _compact_label(label)
    if not stem or len(stem) < 3:
        return None
    for directory in dirs:
        try:
            for child in directory.iterdir():
                if child.suffix.lower() not in IMAGE_EXT:
                    continue
                if _compact_label(child.stem) == stem:
                    return str(child.resolve())
        except Exception:
            continue
    return None


def looks_like_image_label_list(text: str) -> bool:
    if not text or IMAGE_FILE_RE.search(text) or RELATIVE_IMAGE_RE.search(text):
        return False
    main = re.split(r"[—\-–]\s*(?:in that order)?\.?$", text, maxsplit=1)[0]
    parts = [part.strip() for part in re.split(r"[,;]", main) if part.strip()]
    if len(parts) < 2 or len(parts) > 12:
        return False
    for part in parts:
        if re.search(r"\.(?:png|jpe?g|webp|gif|bmp)\b", part, re.I):
            return False
        if len(part) > 48 or len(part.split()) > 5:
            return False
    return True


def paths_from_labels(
    text: str,
    chat_texts: list[str],
    roots: list[Path],
) -> list[str]:
    if not text or not looks_like_image_label_list(text):
        return []
    if not chat_image_context(chat_texts, text):
        return []
    main = re.split(r"[—\-–]\s*(?:in that order)?\.?$", text, maxsplit=1)[0]
    labels = [
        clean_display_token(part)
        for part in re.split(r"[,;]", main)
        if clean_display_token(part)
    ]
    dirs = candidate_image_dirs(chat_texts, roots)
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        path = find_image_by_label(label, dirs)
        if not path:
            continue
        allowed = allowed_chat_image(path, chat_texts + [text])
        if allowed and allowed not in seen:
            seen.add(allowed)
            out.append(allowed)
    return out


def paths_from_loose_filenames(
    text: str,
    chat_texts: list[str],
    roots: list[Path],
) -> list[str]:
    if not text or not LOOSE_FILE_RE.search(text):
        return []
    dirs = candidate_image_dirs(chat_texts, roots)
    if not dirs:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in LOOSE_FILE_RE.findall(text):
        fname = clean_display_token(raw)
        if not fname or not re.search(r"\.(?:png|jpe?g|webp|gif|bmp|tiff?)$", fname, re.I):
            continue
        for directory in dirs:
            candidate = directory / fname
            if not candidate.is_file():
                continue
            allowed = allowed_chat_image(str(candidate), chat_texts)
            if allowed and allowed not in seen:
                seen.add(allowed)
                out.append(allowed)
                break
    return out


def _chat_blob(chat_texts: list[str]) -> str:
    return "\n".join(chat_texts or []).replace("/", "\\")


def path_mentioned_in_chat(path: str, chat_texts: list[str]) -> bool:
    norm = normalize_path(path)
    if not norm:
        return False
    blob = _chat_blob(chat_texts)
    blob_l = blob.lower()
    norm_l = norm.lower()
    if norm_l in blob_l:
        return True
    name = Path(norm).name.lower()
    if name and name in blob_l:
        return True
    stem = _compact_label(Path(norm).stem)
    if stem and stem in _compact_label(blob):
        return True
    rel = str(Path(norm)).lower()
    for part in rel.split("\\"):
        if part and part in blob_l:
            return True
    parent = str(Path(norm).parent)
    if parent.lower() in blob_l and name in blob_l:
        return True
    return False


def allowed_chat_image(path: str, chat_texts: list[str]) -> str | None:
    norm = normalize_path(path)
    if not norm:
        return None
    p = Path(norm)
    if not p.is_file() or p.suffix.lower() not in IMAGE_EXT:
        return None
    resolved, _ = file_browser.file_response_path(norm)
    if resolved:
        return str(resolved)
    if path_mentioned_in_chat(norm, chat_texts):
        try:
            return str(p.resolve())
        except Exception:
            return None
    return None


def list_images_in_dir(dir_path: str, chat_texts: list[str], limit: int = 12) -> list[str]:
    base = Path(normalize_path(dir_path))
    if not base.is_dir():
        return []
    can_list = bool(file_browser.list_dir(str(base)).get("ok"))
    if not can_list and not path_mentioned_in_chat(str(base), chat_texts):
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
        allowed = allowed_chat_image(str(child), chat_texts)
        if allowed and allowed not in out:
            out.append(allowed)
    return out


def ingest_path_token(
    raw: str,
    chat_texts: list[str],
    roots: list[Path],
    seen: set[str],
    out: list[str],
) -> None:
    path = resolve_token_path(raw, roots) or normalize_path(raw)
    if not path:
        return
    p = Path(path)
    if p.is_file() and p.suffix.lower() in IMAGE_EXT:
        allowed = allowed_chat_image(path, chat_texts)
        if allowed and allowed not in seen:
            seen.add(allowed)
            out.append(allowed)
        return
    if p.is_dir():
        for img in list_images_in_dir(path, chat_texts):
            if img not in seen:
                seen.add(img)
                out.append(img)


def paths_from_text(
    text: str,
    chat_texts: list[str] | None = None,
    roots: list[Path] | None = None,
) -> list[str]:
    if not text:
        return []
    texts = chat_texts if chat_texts is not None else [text]
    roots = roots or project_roots()
    seen: set[str] = set()
    ordered: list[str] = []
    for match in IMAGE_FILE_RE.finditer(text):
        ingest_path_token(match.group(0), texts, roots, seen, ordered)
    for match in RELATIVE_IMAGE_RE.finditer(text):
        ingest_path_token(match.group(0), texts, roots, seen, ordered)
    for match in MARKDOWN_IMAGE_RE.finditer(text):
        ingest_path_token(match.group(1), texts, roots, seen, ordered)
    for match in GLOB_DIR_RE.finditer(text):
        ingest_path_token(match.group(1), texts, roots, seen, ordered)
    for match in RELATIVE_DIR_RE.finditer(text):
        token = _clean_token(match.group(0))
        if IMAGE_FILE_RE.search(token) or RELATIVE_IMAGE_RE.search(token):
            continue
        ingest_path_token(token, texts, roots, seen, ordered)
    for match in DIR_RE.finditer(text):
        token = _clean_token(match.group(0))
        if IMAGE_FILE_RE.search(token) or RELATIVE_IMAGE_RE.search(token):
            continue
        ingest_path_token(token, texts, roots, seen, ordered)
    for path in paths_from_loose_filenames(text, texts, roots):
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    for path in paths_from_labels(text, texts, roots):
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return prefer_display_paths(ordered)


def image_record(path: str, alt: str = "", source: str = "path") -> dict[str, Any]:
    name = Path(path).name
    clean_alt = clean_display_token(alt) if alt else name
    if clean_alt and not re.search(r"\.(?:png|jpe?g|webp|gif|bmp|tiff?)$", clean_alt, re.I):
        clean_alt = name
    return {
        "id": path,
        "src": f"local:{path}",
        "path": path,
        "alt": clean_alt or name,
        "source": source,
    }


def enrich_message_images(
    message: dict[str, Any],
    chat_texts: list[str],
    roots: list[Path],
) -> None:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_path(path: str, alt: str = "", source: str = "path") -> None:
        resolved = resolve_token_path(path, roots) or normalize_path(path)
        allowed = allowed_chat_image(resolved or path, chat_texts)
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
        local = None
        for candidate in (path, src, alt):
            if not candidate:
                continue
            resolved = resolve_token_path(candidate, roots) or normalize_path(candidate)
            if resolved and Path(resolved).suffix.lower() in IMAGE_EXT:
                local = allowed_chat_image(resolved, chat_texts)
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

    for path in paths_from_text(str(message.get("text") or ""), chat_texts, roots):
        add_path(path, source="text")
    for path in paths_from_labels(str(message.get("text") or ""), chat_texts, roots):
        add_path(path, source="label")
    for path in paths_from_loose_filenames(str(message.get("text") or ""), chat_texts, roots):
        add_path(path, source="loose")

    remote: list[dict[str, Any]] = []
    local_paths: list[str] = []
    local_records: dict[str, dict[str, Any]] = {}
    for img in merged:
        path = str(img.get("path") or "")
        src = str(img.get("src") or "")
        if path:
            local_paths.append(path)
            local_records[path] = img
        elif src.startswith("http://") or src.startswith("https://") or src.startswith("data:image/"):
            remote.append(img)
    preferred = prefer_display_paths(local_paths)
    message["images"] = [local_records[p] for p in preferred if p in local_records] + remote
    message["images"] = message["images"][:20]


def enrich_state_images(state: dict[str, Any]) -> None:
    texts = [str(m.get("text") or "") for m in state.get("messages") or [] if isinstance(m, dict)]
    roots = project_roots(state)
    for message in state.get("messages") or []:
        if isinstance(message, dict):
            enrich_message_images(message, texts, roots)


def allowed_image_paths(state: dict[str, Any]) -> set[str]:
    texts = [str(m.get("text") or "") for m in state.get("messages") or [] if isinstance(m, dict)]
    roots = project_roots(state)
    allowed: set[str] = set()
    for message in state.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for img in message.get("images") or []:
            path = str(img.get("path") or "")
            if path:
                allowed.add(normalize_path(path))
            src = str(img.get("src") or "")
            if src.startswith("local:"):
                allowed.add(normalize_path(src[6:]))
        for path in paths_from_text(str(message.get("text") or ""), texts, roots):
            allowed.add(normalize_path(path))
        for path in paths_from_labels(str(message.get("text") or ""), texts, roots):
            allowed.add(normalize_path(path))
        for path in paths_from_loose_filenames(str(message.get("text") or ""), texts, roots):
            allowed.add(normalize_path(path))
    return allowed
