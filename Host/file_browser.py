"""Safe file browse / preview / Explorer open for CursorDesk phone UI."""

from __future__ import annotations

import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Any

PROJECT = Path(
    os.environ.get(
        "CURSORDESK_PROJECT",
        str(Path.home() / "Documents"),
    )
)
HOME = Path(os.path.expanduser("~"))

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tga"}
TEXT_EXT = {".txt", ".md", ".json", ".ini", ".log", ".csv", ".py", ".js", ".ts", ".css", ".html"}


def _roots() -> list[dict[str, str]]:
    shots = PROJECT / "Saved" / "Screenshots"
    try:
        shots.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    candidates = [
        ("Project", PROJECT),
        ("UE Screenshots", shots),
        ("UE Saved", PROJECT / "Saved"),
        ("Win Screenshots", HOME / "Pictures" / "Screenshots"),
        ("Desktop", HOME / "Desktop"),
        ("Downloads", HOME / "Downloads"),
        ("Pictures", HOME / "Pictures"),
    ]
    out: list[dict[str, str]] = []
    for label, path in candidates:
        try:
            if path.exists():
                out.append({"id": label, "label": label, "path": str(path.resolve())})
        except Exception:
            continue
    return out


def _allowed_roots() -> list[Path]:
    roots: list[Path] = []
    for r in _roots():
        try:
            roots.append(Path(r["path"]).resolve())
        except Exception:
            continue
    # Also allow anything under the user profile + project
    for extra in (HOME, PROJECT):
        try:
            roots.append(extra.resolve())
        except Exception:
            pass
    return roots


def _is_allowed(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    if not resolved.exists():
        return False
    for root in _allowed_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _looks_like_project(path: Path) -> bool:
    if not path.is_dir():
        return False
    markers = (
        ".git",
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "go.mod",
        "CMakeLists.txt",
        "CursorDesk",
    )
    for name in markers:
        if (path / name).exists():
            return True
    try:
        if any(path.glob("*.uproject")):
            return True
        if any(path.glob("*.sln")):
            return True
    except Exception:
        pass
    return False


def list_workspace_candidates(limit: int = 48) -> dict[str, Any]:
    """Likely project folders for new-agent workspace picker."""
    scan_roots: list[Path] = []
    for raw in (
        PROJECT,
        PROJECT.parent,
        HOME / "Documents" / "GitHub",
        HOME / "Documents" / "Unreal Projects",
        HOME / "Documents",
        HOME / "source" / "repos",
    ):
        try:
            resolved = raw.resolve()
            if resolved.is_dir() and resolved not in scan_roots:
                scan_roots.append(resolved)
        except Exception:
            continue
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in scan_roots:
        try:
            if _looks_like_project(root):
                key = str(root).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(
                        {
                            "path": str(root),
                            "name": root.name,
                            "label": root.name,
                        }
                    )
            for child in root.iterdir():
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if not _looks_like_project(child):
                    continue
                key = str(child.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "path": str(child.resolve()),
                        "name": child.name,
                        "label": child.name,
                    }
                )
        except Exception:
            continue
    found.sort(key=lambda item: item["name"].lower())
    return {"ok": True, "workspaces": found[:limit], "roots": _roots()}


def list_dir(path_str: str | None = None) -> dict[str, Any]:
    if not path_str:
        return {"ok": True, "path": None, "roots": _roots(), "entries": []}
    path = Path(path_str)
    if not _is_allowed(path) or not path.is_dir():
        return {"ok": False, "error": "Path not allowed or not a folder", "path": path_str}
    entries: list[dict[str, Any]] = []
    try:
        children = list(path.iterdir())
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in children[:500]:
        try:
            st = child.stat()
            ext = child.suffix.lower()
            entries.append(
                {
                    "name": child.name,
                    "path": str(child.resolve()),
                    "isDir": child.is_dir(),
                    "size": st.st_size if child.is_file() else 0,
                    "mtime": int(st.st_mtime),
                    "isImage": ext in IMAGE_EXT,
                    "isText": ext in TEXT_EXT,
                }
            )
        except Exception:
            continue
    parent = str(path.parent.resolve()) if path.parent != path else None
    return {
        "ok": True,
        "path": str(path.resolve()),
        "parent": parent if parent and _is_allowed(Path(parent)) else None,
        "entries": entries,
        "roots": _roots(),
    }


def open_explorer(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not _is_allowed(path):
        return {"ok": False, "error": "Path not allowed"}
    target = path if path.is_dir() else path.parent
    try:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path.resolve())])
        else:
            subprocess.Popen(["explorer", str(target.resolve())])
        return {"ok": True, "path": str(path.resolve())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def open_on_pc(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not _is_allowed(path) or not path.is_file():
        return {"ok": False, "error": "File not allowed"}
    try:
        os.startfile(str(path.resolve()))  # type: ignore[attr-defined]
        return {"ok": True, "path": str(path.resolve())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def file_response_path(path_str: str) -> tuple[Path | None, str | None]:
    path = Path(path_str)
    if not _is_allowed(path) or not path.is_file():
        return None, "File not allowed"
    return path.resolve(), None


def guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".gif":
        return "image/gif"
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"
