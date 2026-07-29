"""Capture sanitized CursorDesk screenshots for public README.

Run while the host is up:  python capture_screenshots.py

Strips real chat/desktop content before capture so you can commit docs safely.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8765"

SANITIZE = """
(() => {
  const meta = document.getElementById('meta');
  if (meta) meta.textContent = 'My Project · live';
  const sub = document.getElementById('drawerSub');
  if (sub) sub.textContent = 'CursorDesk · CDP';
  const runner = document.getElementById('chatRunner');
  if (runner) { runner.classList.remove('show'); runner.innerHTML = ''; }
  const banner = document.getElementById('agentBanner');
  if (banner) banner.classList.remove('show');
  const log = document.getElementById('chatLog');
  if (log) {
    log.innerHTML = `
      <div class="msg human"><div class="role">You · tap to edit</div><div class="body">Fix the login bug in auth.ts</div></div>
      <div class="msg assistant"><div class="body">I'll trace the session handler and patch the redirect loop.</div><div class="msgTime">2m ago</div></div>`;
  }
  const files = document.getElementById('filesList');
  if (files) {
    files.innerHTML = `
      <div class="frow"><div class="ico">📁</div><div><div class="name">src</div><div class="meta">folder</div></div><div></div></div>
      <div class="frow"><div class="ico">📄</div><div><div class="name">README.md</div><div class="meta">4.2 KB</div></div><div></div></div>
      <div class="frow"><div class="ico">📄</div><div><div class="name">package.json</div><div class="meta">1.1 KB</div></div><div></div></div>`;
  }
  const path = document.getElementById('filesPath');
  if (path) path.textContent = 'Documents / my-app';
  const canvas = document.getElementById('frame');
  if (canvas) {
    const ctx = canvas.getContext('2d');
    canvas.width = 780; canvas.height = 520;
    const g = ctx.createLinearGradient(0, 0, 780, 520);
    g.addColorStop(0, '#0d1210');
    g.addColorStop(1, '#141c18');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 780, 520);
    ctx.fillStyle = 'rgba(115,242,184,.75)';
    ctx.font = '600 22px Segoe UI, sans-serif';
    ctx.fillText('Cursor IDE window stream', 42, 72);
    ctx.fillStyle = 'rgba(232,255,244,.45)';
    ctx.font = '14px Consolas, monospace';
    ctx.fillText('// Your actual Cursor window appears here', 42, 108);
    ctx.fillText('// Touch, pinch-zoom, and pan on phone', 42, 132);
  }
})();
"""


def shot(page, name: str) -> None:
    path = OUT / name
    page.screenshot(path=str(path), full_page=False)
    print("wrote", path)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
    )
    page.goto(BASE, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1200)
    page.evaluate(SANITIZE)
    page.wait_for_timeout(300)
    shot(page, "agent-tab.png")

    page.click("#tabDesktop")
    page.wait_for_timeout(600)
    page.evaluate(SANITIZE)
    shot(page, "desktop-tab.png")

    page.click("#tabFiles")
    page.wait_for_timeout(600)
    page.evaluate(SANITIZE)
    shot(page, "files-tab.png")

    browser.close()
