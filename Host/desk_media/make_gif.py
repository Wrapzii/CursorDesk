"""Create animated GIFs from local image frames for CursorDesk phone preview."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

import file_browser

FRAME_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif"}


def default_gif_dir() -> Path:
    base = Path(os.environ.get("CURSORDESK_PROJECT", str(file_browser.PROJECT)))
    out = base / "Saved" / "GIFs"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _resolve_frame(path: str) -> Path:
    resolved, err = file_browser.file_response_path(path)
    if resolved is None:
        raise FileNotFoundError(err or f"frame not allowed or missing: {path}")
    if resolved.suffix.lower() not in FRAME_EXT:
        raise ValueError(f"unsupported frame type: {resolved.suffix}")
    return resolved


def _load_frame(path: Path, size: tuple[int, int] | None) -> Image.Image:
    with Image.open(path) as img:
        frame = img.convert("RGBA")
    if size and frame.size != size:
        frame = frame.resize(size, Image.Resampling.LANCZOS)
    return frame


def _resolve_output(path: Path) -> Path:
    path = path.with_suffix(".gif")
    if path.is_absolute():
        try:
            resolved = path.resolve()
            parent = resolved.parent
            if parent.exists():
                allowed, _ = file_browser.file_response_path(str(parent))
                if allowed is not None:
                    return resolved
        except Exception:
            pass
    name = path.name or f"clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif"
    return default_gif_dir() / name


def create_gif_from_paths(
    frame_paths: list[str],
    output_path: str | Path | None = None,
    *,
    duration_ms: int = 200,
    loop: int = 0,
) -> Path:
    if not frame_paths:
        raise ValueError("at least one frame path is required")
    if duration_ms < 20:
        raise ValueError("duration_ms must be at least 20")

    resolved = [_resolve_frame(path) for path in frame_paths]
    base_size = Image.open(resolved[0]).size
    frames = [_load_frame(path, base_size) for path in resolved]

    out = _resolve_output(Path(output_path)) if output_path else default_gif_dir() / f"clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif"
    out = out.with_suffix(".gif")
    out.parent.mkdir(parents=True, exist_ok=True)

    # Flatten to palette-friendly RGB for smaller files and broad phone support.
    rgb_frames: list[Image.Image] = []
    for frame in frames:
        bg = Image.new("RGBA", frame.size, (0, 0, 0, 255))
        bg.alpha_composite(frame)
        rgb_frames.append(bg.convert("RGB"))

    first, rest = rgb_frames[0], rgb_frames[1:]
    first.save(
        out,
        format="GIF",
        save_all=True,
        append_images=rest,
        duration=duration_ms,
        loop=loop,
        optimize=True,
    )
    for frame in frames:
        frame.close()
    for frame in rgb_frames:
        frame.close()
    return out.resolve()


def collect_frames_from_dir(directory: str, pattern: str = "*") -> list[str]:
    base = Path(directory)
    resolved, err = file_browser.file_response_path(str(base))
    if resolved is None or not resolved.is_dir():
        raise FileNotFoundError(err or f"directory not allowed: {directory}")
    rx = re.compile(
        "^" + re.escape(pattern).replace("\\*", ".*").replace("\\?", ".") + "$",
        re.IGNORECASE,
    )
    matches = [
        str(child.resolve())
        for child in sorted(resolved.iterdir())
        if child.is_file() and child.suffix.lower() in FRAME_EXT and rx.match(child.name)
    ]
    if not matches:
        raise FileNotFoundError(f"no frames matched {pattern!r} in {resolved}")
    return matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a GIF from local image frames.")
    parser.add_argument("--frames", nargs="*", help="Ordered image paths")
    parser.add_argument("--from-dir", dest="from_dir", help="Directory to scan for frames")
    parser.add_argument("--pattern", default="*", help="Glob-style filename filter for --from-dir")
    parser.add_argument("-o", "--output", help="Output .gif path (default: Saved/GIFs/clip_*.gif)")
    parser.add_argument("--duration-ms", type=int, default=200, help="Frame duration in ms")
    parser.add_argument("--loop", type=int, default=0, help="Loop count (0 = forever)")
    args = parser.parse_args(argv)

    try:
        frames = list(args.frames or [])
        if args.from_dir:
            frames.extend(collect_frames_from_dir(args.from_dir, args.pattern))
        out = create_gif_from_paths(
            frames,
            args.output,
            duration_ms=args.duration_ms,
            loop=args.loop,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
