"""Optional stdio MCP server for CursorDesk media helpers (NOT Unreal REAgentTools).

This is separate from the REAgentTools Unreal plugin (GitHub/REAgentTools).
Prefer RECaptureWorkflowTools.make_gif_from_frames inside Unreal when possible.

Example Cursor MCP entry:
{
  "cursordesk-media": {
    "command": "python",
    "args": ["C:/path/to/CursorDesk/Host/desk_media/mcp_server.py"],
    "cwd": "C:/path/to/CursorDesk/Host"
  }
}
"""

from __future__ import annotations

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional dependency
    raise SystemExit(
        "Install the MCP SDK first: pip install mcp\n"
        f"Original error: {exc}"
    ) from exc

from desk_media.make_gif import collect_frames_from_dir, create_gif_from_paths

mcp = FastMCP("cursordesk-media")


@mcp.tool()
def make_gif(
    frame_paths: list[str],
    output_name: str = "",
    duration_ms: int = 200,
    loop: int = 0,
) -> str:
    """Create an animated GIF from ordered local image paths.

    Returns the absolute output path. Paste that path in chat so CursorDesk embeds it on phone.
    """
    output = output_name.strip() or None
    out = create_gif_from_paths(
        frame_paths,
        output,
        duration_ms=duration_ms,
        loop=loop,
    )
    return str(out)


@mcp.tool()
def make_gif_from_directory(
    directory: str,
    pattern: str = "*.png",
    output_name: str = "",
    duration_ms: int = 200,
    loop: int = 0,
) -> str:
    """Create a GIF from all matching images in a directory (sorted by name)."""
    frames = collect_frames_from_dir(directory, pattern)
    output = output_name.strip() or None
    out = create_gif_from_paths(
        frames,
        output,
        duration_ms=duration_ms,
        loop=loop,
    )
    return str(out)


if __name__ == "__main__":
    mcp.run()
