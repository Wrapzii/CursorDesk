"""Quick tests for local agent image embedding."""
from pathlib import Path

from image_resolver import enrich_message_images, paths_from_text

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"


def test_directory_expansion():
    text = f"Saved renders in `{SHOTS}`"
    paths = paths_from_text(text)
    assert paths, f"expected images from {SHOTS}"
    print("directory expansion OK:", len(paths), "images")


def test_file_path():
    sample = SHOTS / "agent-tab.png"
    if not sample.exists():
        print("skip file path test - screenshot missing")
        return
    text = f"Here is the preview: {sample}"
    paths = paths_from_text(text)
    assert str(sample.resolve()) in paths
    print("file path OK:", paths[0])


def test_message_enrich():
    sample = SHOTS / "agent-tab.png"
    if not sample.exists():
        print("skip enrich test")
        return
    msg = {
        "type": "assistant",
        "text": f"Output folder: {SHOTS}",
        "images": [{"src": "broken://cursor-image", "alt": str(sample)}],
    }
    enrich_message_images(msg)
    assert msg["images"], "expected enriched images"
    assert msg["images"][0].get("path")
    print("enrich OK:", msg["images"][0]["path"])


if __name__ == "__main__":
    test_file_path()
    test_directory_expansion()
    test_message_enrich()
    print("all image resolver tests passed")
