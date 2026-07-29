"""Quick tests for local agent image embedding."""
from pathlib import Path

from image_resolver import (
    clean_display_token,
    enrich_message_images,
    paths_from_text,
    paths_from_labels,
    prefer_display_paths,
    project_roots,
)

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"


def test_directory_expansion():
    text = f"Saved renders in `{SHOTS}`"
    paths = paths_from_text(text, [text])
    assert paths, f"expected images from {SHOTS}"
    print("directory expansion OK:", len(paths), "images")


def test_file_path():
    sample = SHOTS / "agent-tab.png"
    if not sample.exists():
        print("skip file path test - screenshot missing")
        return
    text = f"Here is the preview: {sample}"
    paths = paths_from_text(text, [text])
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
    enrich_message_images(msg, [msg["text"]], project_roots())
    assert msg["images"], "expected enriched images"
    assert msg["images"][0].get("path")
    print("enrich OK:", msg["images"][0]["path"])


def test_label_list():
    msgs = [
        {"text": "show me the images"},
        {"text": "Firebolt, Ice Wall, Aegis Shield, Ember Edge — in that order."},
    ]
    texts = [m["text"] for m in msgs]
    paths = paths_from_labels(msgs[1]["text"], texts, project_roots())
    assert paths, "expected label list to resolve FXShots images"
    print("label list OK:", len(paths), "images")


def test_clean_display_token():
    assert clean_display_token("AegisShield.png — 1,173,227 bytes") == "AegisShield.png"
    assert clean_display_token("Firebolt 2m ago") == "Firebolt"
    assert clean_display_token("IceWall.jpg — 64,532 bytes") == "IceWall.jpg"
    assert clean_display_token(
        "Eight files. All written 7/29/2026 3:14:38 PM"
    ) == "Eight files"
    print("clean token OK")


def test_prefer_view_jpegs():
    png = r"C:\proj\Saved\FXShots\Firebolt.png"
    jpg = r"C:\proj\Saved\FXShots\view\Firebolt.jpg"
    out = prefer_display_paths([png, jpg])
    assert out == [jpg]
    print("prefer view jpeg OK")


if __name__ == "__main__":
    test_file_path()
    test_directory_expansion()
    test_message_enrich()
    test_label_list()
    test_clean_display_token()
    test_prefer_view_jpegs()
    print("all image resolver tests passed")
