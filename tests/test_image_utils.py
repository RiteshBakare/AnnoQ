import hashlib
import io

import pytest
from PIL import Image

from src.image_utils import InputError, image_parts, load_flow, name_hint, natural_key, scan_flow
from tests.conftest import png_bytes


def test_natural_sort_and_ignored_files(make_flow_dir):
    d = make_flow_dir(["10_success.png", "2_shipping.JPG", "1_cart.png", "03_pay.webp"])
    (d / ".hidden.png").write_bytes(png_bytes())
    (d / "notes.txt").write_text("x")
    (d / "sub").mkdir()
    (d / "sub" / "0_inner.png").write_bytes(png_bytes())
    assert [p.name for p in scan_flow(d, 8)] == ["1_cart.png", "2_shipping.JPG", "03_pay.webp", "10_success.png"]


def test_natural_key():
    assert sorted(["s10.png", "s2.png", "S1.png"], key=natural_key) == ["S1.png", "s2.png", "s10.png"]


@pytest.mark.parametrize("name, hint", [("01_cart.png", "Cart"), ("03_place-order.png", "Place order"),
                                        ("10 - order success.jpg", "Order success"), ("007.png", "")])
def test_name_hint(name, hint):
    assert name_hint(name) == hint


def test_too_many_screens(make_flow_dir):
    d = make_flow_dir([f"{i}.png" for i in range(9)])
    with pytest.raises(InputError, match="split this flow"):
        scan_flow(d, 8)


def test_empty_folder_and_missing_path(tmp_path):
    with pytest.raises(InputError, match="No PNG"):
        scan_flow(tmp_path, 8)
    with pytest.raises(InputError, match="not found"):
        scan_flow(tmp_path / "nope", 8)


def test_single_file_mode(tmp_path):
    f = tmp_path / "login.png"
    f.write_bytes(png_bytes())
    flow = load_flow(f)
    assert flow.hint == "login" and [s.id for s in flow.screens] == ["S1"]
    bad = tmp_path / "doc.pdf"
    bad.write_bytes(b"%PDF")
    with pytest.raises(InputError, match="Unsupported"):
        scan_flow(bad, 8)


def test_load_flow_ids_hashes_marker(make_flow_dir):
    d = make_flow_dir(["01_cart.png", "02_shipping.png"])
    flow = load_flow(d)
    assert flow.hint == "shopping"
    assert [(s.id, s.file, s.hint) for s in flow.screens] == [("S1", "01_cart.png", "Cart"),
                                                              ("S2", "02_shipping.png", "Shipping")]
    s1 = flow.screens[0]
    assert s1.sha256 == hashlib.sha256((d / "01_cart.png").read_bytes()).hexdigest()
    assert flow.marker.startswith("ai-screen-questions:v2:") and len(flow.marker.split(":")[-1]) == 12
    assert load_flow(d).sha == flow.sha  # deterministic
    (d / "01_cart.png").rename(d / "03_cart.png")  # order change -> different flow hash
    assert load_flow(d).sha != flow.sha


def test_downscale_keeps_original_hash(make_flow_dir):
    d = make_flow_dir(["tall.png"], sizes={"tall.png": (1000, 3000)})
    s = load_flow(d, max_edge=2000).screens[0]
    assert s.resized and (s.width, s.height) == (667, 2000)
    assert Image.open(io.BytesIO(s.data)).size == (667, 2000)
    assert s.sha256 == hashlib.sha256((d / "tall.png").read_bytes()).hexdigest()
    small = load_flow(d, max_edge=4000).screens[0]
    assert not small.resized and small.data == (d / "tall.png").read_bytes()


def test_oversize_and_corrupt_images(make_flow_dir):
    d = make_flow_dir(["a.png"])
    with pytest.raises(InputError, match="MAX_IMAGE_MB"):
        load_flow(d, max_mb=0.0001)
    (d / "a.png").write_bytes(b"not an image")
    with pytest.raises(InputError, match="Cannot read image a.png"):
        load_flow(d)


def test_image_parts_labelled_in_order(make_flow_dir):
    flow = load_flow(make_flow_dir(["1.png", "2.png"]))
    parts = image_parts(flow)
    assert [p.text for p in parts[::2]] == ['Screen S1 (file "1.png"):', 'Screen S2 (file "2.png"):']
    assert all(p.inline_data and p.inline_data.mime_type == "image/png" for p in parts[1::2])
