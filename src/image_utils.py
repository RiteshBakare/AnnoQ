"""Flow input: folder scan (natural sort = flow order), per-image validation/downscale, hashes, Gemini Parts."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .config import ConfigError

IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_PIL_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
MARKER_PREFIX = "ai-screen-questions:v2:"


class InputError(ConfigError):
    """Bad flow input (missing/empty folder, too many screens, unreadable image). Exit code 1."""


@dataclass(frozen=True)
class ScreenImage:
    id: str  # S1..Sn in flow order
    file: str  # file name
    hint: str  # filename-derived name hint, e.g. "Place order"
    mime_type: str
    data: bytes  # bytes sent to Gemini (downscaled if needed)
    sha256: str  # hash of the ORIGINAL file bytes (stable marker regardless of resize settings)
    width: int
    height: int
    resized: bool


@dataclass(frozen=True)
class Flow:
    hint: str  # folder name (or file stem for a single image)
    screens: list[ScreenImage]
    sha: str  # sha256 over the ordered per-image hashes

    @property
    def marker(self) -> str:
        return f"{MARKER_PREFIX}{self.sha[:12]}"


def natural_key(name: str) -> list:
    """'2_b.png' < '10_a.png': digit runs compare as numbers, the rest case-insensitively."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", name)]


def name_hint(filename: str) -> str:
    """'03_place-order.png' -> 'Place order'. Empty if the stem is only digits/separators."""
    stem = re.sub(r"^[\d\s._-]+", "", Path(filename).stem)
    words = re.sub(r"[_\-.\s]+", " ", stem).strip()
    return words[:1].upper() + words[1:] if words else ""


def scan_flow(path: str | Path, max_screens: int) -> list[Path]:
    """Folder -> image files in natural order (non-recursive, hidden ignored); single image -> [it]."""
    p = Path(path)
    if not p.exists():
        raise InputError(f"Input path not found: {p}")
    if p.is_file():
        if p.suffix.lower() not in IMAGE_EXTS:
            raise InputError(f"Unsupported image type {p.suffix!r} for {p.name}; use PNG, JPG, JPEG or WEBP.")
        return [p]
    files = sorted(
        (f for f in p.iterdir()
         if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in IMAGE_EXTS),
        key=lambda f: natural_key(f.name),
    )
    if not files:
        raise InputError(f"No PNG/JPG/JPEG/WEBP images found in {p}.")
    if len(files) > max_screens:
        raise InputError(f"{len(files)} screens in {p} exceeds MAX_SCREENS={max_screens}; "
                         "split this flow across tickets.")
    return files


def prepare_image(path: Path, max_mb: float, max_edge: int) -> tuple[bytes, str, int, int, bool, str]:
    """Validate and downscale (long edge <= max_edge). Returns (data, mime, w, h, resized, sha_of_original)."""
    raw = path.read_bytes()
    if len(raw) > max_mb * 1024 * 1024:
        raise InputError(f"{path.name} is {len(raw) / 1048576:.1f} MB; MAX_IMAGE_MB={max_mb}.")
    sha = hashlib.sha256(raw).hexdigest()
    mime = IMAGE_EXTS[path.suffix.lower()]
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            w, h = img.size
            if max(w, h) <= max_edge:
                return raw, mime, w, h, False, sha
            img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            if mime == "image/jpeg" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, _PIL_FORMAT[mime], **({"quality": 90} if mime != "image/png" else {}))
            return buf.getvalue(), mime, img.width, img.height, True, sha
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise InputError(f"Cannot read image {path.name}: {e}") from None


def load_flow(path: str | Path, max_screens: int = 8, max_mb: float = 10, max_edge: int = 2000) -> Flow:
    p = Path(path)
    screens = []
    for k, f in enumerate(scan_flow(p, max_screens), start=1):
        data, mime, w, h, resized, sha = prepare_image(f, max_mb, max_edge)
        screens.append(ScreenImage(id=f"S{k}", file=f.name, hint=name_hint(f.name), mime_type=mime,
                                   data=data, sha256=sha, width=w, height=h, resized=resized))
    flow_sha = hashlib.sha256("".join(s.sha256 for s in screens).encode()).hexdigest()
    return Flow(hint=p.stem if p.is_file() else p.resolve().name, screens=screens, sha=flow_sha)


def image_parts(flow: Flow) -> list:
    """Gemini parts in flow order. Each image is preceded by a short label so ids map to images unambiguously."""
    from google.genai import types

    parts = []
    for s in flow.screens:
        parts.append(types.Part(text=f"Screen {s.id} (file \"{s.file}\"):"))
        parts.append(types.Part.from_bytes(data=s.data, mime_type=s.mime_type))
    return parts
