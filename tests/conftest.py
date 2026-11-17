import io
from pathlib import Path

import pytest
from PIL import Image


def png_bytes(w: int = 40, h: int = 80, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def make_flow_dir(tmp_path: Path):
    """make_flow_dir(["01_cart.png", ...]) -> folder with small distinct PNGs."""

    def _make(names, sizes=None):
        d = tmp_path / "shopping"
        d.mkdir(exist_ok=True)
        colors = ["red", "green", "blue", "orange", "purple", "black", "white", "gray", "yellow", "pink"]
        for i, n in enumerate(names):
            w, h = (sizes or {}).get(n, (40, 80))
            (d / n).write_bytes(png_bytes(w, h, colors[i % len(colors)]))
        return d

    return _make
