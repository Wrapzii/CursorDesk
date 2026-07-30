"""Tests for GIF creation helper."""

from pathlib import Path

from PIL import Image

from desk_media.make_gif import create_gif_from_paths


def _frame(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (24, 16), color).save(path)


def test_create_gif_from_paths(tmp_path: Path):
    frames = []
    for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
        p = tmp_path / f"f{i}.png"
        _frame(p, color)
        frames.append(str(p))
    out = create_gif_from_paths(frames, tmp_path / "out.gif", duration_ms=80)
    assert out.exists()
    assert out.stat().st_size > 100
    with Image.open(out) as gif:
        assert gif.format == "GIF"
        assert getattr(gif, "n_frames", 1) >= 3
    print("gif create OK:", out)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        test_create_gif_from_paths(Path(td))
    print("all gif tests passed")
