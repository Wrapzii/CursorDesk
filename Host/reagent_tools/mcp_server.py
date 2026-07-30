"""Deprecated — use desk_media/mcp_server.py (cursordesk-media), not Unreal REAgentTools."""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "desk_media" / "mcp_server.py"
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
