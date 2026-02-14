import os
import re
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from modules.manga_formatter.xtc import build_xtc, build_single_page_xtc


DEFAULT_SETTINGS = {
    "dithering": True,
    "contrast": 4,
    "target_width": 480,
    "target_height": 800,
}


def _parse_settings(raw):
    s = dict(DEFAULT_SETTINGS)
    if raw:
        if "dithering" in raw:
            s["dithering"] = bool(raw["dithering"])
        if "contrast" in raw:
            s["contrast"] = max(0, min(8, int(raw["contrast"])))
        if "target_width" in raw:
            s["target_width"] = int(raw["target_width"])
        if "target_height" in raw:
            s["target_height"] = int(raw["target_height"])
    return s

_CH_PATTERNS = [
    re.compile(r'chapter\s*[._-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
    re.compile(r'chp\s*[._-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
    re.compile(r'ch\s*[._-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE),
]

_VOL_NOISE = re.compile(
    r'(?:vol(?:ume)?|book|bk)\s*[._-]?\s*\d+(?:\.\d+)?',
    re.IGNORECASE,
)


def extract_chapter_number(filename):
    base = os.path.splitext(os.path.basename(filename))[0]

    for pat in _CH_PATTERNS:
        m = pat.search(base)
        if m:
            num_str = m.group(1)
            if '.' in num_str:
                return int(num_str.split('.')[0]), True
            return int(num_str), False

    cleaned = _VOL_NOISE.sub('', base)
    numbers = re.findall(r'(?<!\d)(\d+(?:\.\d+)?)(?!\d)', cleaned)
    if numbers:
        num_str = numbers[-1]
        if '.' in num_str:
            return int(num_str.split('.')[0]), True
        return int(num_str), False

    return None, False


def classify_cbz_files(cbz_paths):
    recognized = {}
    unrecognized = []

    for path in cbz_paths:
        ch_num, is_decimal = extract_chapter_number(path)
        if ch_num is None:
            unrecognized.append(path)
        elif is_decimal and ch_num in recognized:
            continue
        elif ch_num in recognized:
            unrecognized.append(path)
        else:
            recognized[ch_num] = path

    return recognized, unrecognized

def _apply_contrast(img, level):
    if level == 0:
        return img
    black_cutoff = 3 * level
    white_cutoff = 3 + 9 * level
    return ImageOps.autocontrast(img, cutoff=(black_cutoff, white_cutoff), preserve_tone=True)


def _to_grayscale(img):
    if img.mode == "P":
        img = img.convert("RGB")
    if img.mode != "L":
        img = img.convert("L")
    return img


def _resize_and_pad(img, tw, th, dithering=True):
    iw, ih = img.size
    scale = min(tw / iw, th / ih)
    nw, nh = int(iw * scale), int(ih * scale)

    resized = img.resize((nw, nh), Image.LANCZOS)

    if dithering:
        resized = resized.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")

    canvas = Image.new("L", (tw, th), 255)
    canvas.paste(resized, ((tw - nw) // 2, (th - nh) // 2))
    return canvas

def _process_main_page(img, settings):
    img = _to_grayscale(img)
    img = _apply_contrast(img, settings["contrast"])
    return _resize_and_pad(img, settings["target_width"], settings["target_height"],
                           dithering=settings["dithering"])

def _process_zoom_page(img, settings):
    img = _to_grayscale(img)
    img = _apply_contrast(img, settings["contrast"])

    tw = settings["target_width"]
    th = settings["target_height"]
    width, height = img.size

    desired_segments = 3
    established_scale = th * 1.0 / width
    overlapping_height = tw / established_scale

    num_segments = desired_segments
    min_overlap_pct = 5

    if num_segments > 1:
        shift = overlapping_height - (overlapping_height * num_segments - height) / (num_segments - 1)
    else:
        shift = 0

    while num_segments < 26 and shift > 0 and (shift / overlapping_height) > (1.0 - 0.01 * min_overlap_pct):
        num_segments += 1
        if num_segments > 1:
            shift = overlapping_height - (overlapping_height * num_segments - height) / (num_segments - 1)
        else:
            shift = 0

    segments = []
    for v in range(num_segments):
        top = int(shift * v)
        bottom = int(height - shift * (num_segments - v - 1))
        segment = img.crop((0, top, width, bottom))
        rotated = segment.rotate(-90, expand=True)
        processed = _resize_and_pad(rotated, tw, th, dithering=settings["dithering"])
        segments.append(processed)

    return segments

def _extract_images(cbz_path):
    images = []
    with zipfile.ZipFile(cbz_path, "r") as zf:
        names = zf.namelist()
        exts = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
        img_files = sorted(
            f for f in names
            if f.lower().endswith(exts) and not f.lower().startswith("__macos")
        )
        for f in img_files:
            data = zf.read(f)
            img = Image.open(BytesIO(data))
            if img.mode == "P":
                img = img.convert("RGB")
            images.append(img)
    return images


def get_cbz_preview(cbz_path, max_size=(300, 500)):
    images = _extract_images(cbz_path)
    if not images:
        return None
    img = images[0]
    img.thumbnail(max_size, Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()

def convert_chapter(cbz_path, ch_num, root, settings):
    s = _parse_settings(settings)
    force_size = (s["target_width"], s["target_height"])
    ch_label = f"{ch_num:04d}"
    ch_dir = root / ch_label
    ch_dir.mkdir(parents=True, exist_ok=True)
    zoom_dir = ch_dir / f"zoom_{ch_label}"
    zoom_dir.mkdir(parents=True, exist_ok=True)

    images = _extract_images(cbz_path)

    main_pages = [_process_main_page(img, s) for img in images]
    build_xtc(main_pages, str(ch_dir / f"main_{ch_label}.xtc"), force_size)

    for page_idx, img in enumerate(images, start=1):
        splits = _process_zoom_page(img, s)
        fname = f"{ch_label}_{page_idx}.xtc"
        build_xtc(splits, str(zoom_dir / fname), force_size)


def convert_chapters(chapter_map, output_dir, manga_title, settings=None):
    root = Path(output_dir) / manga_title
    root.mkdir(parents=True, exist_ok=True)

    total = len(chapter_map)
    for idx, (ch_num, cbz_path) in enumerate(sorted(chapter_map.items()), start=1):
        fname = os.path.basename(cbz_path)
        yield {
            "current": idx,
            "total": total,
            "message": f"Processing chapter {ch_num}...",
            "filename": fname
        }

        convert_chapter(cbz_path, ch_num, root, settings)

    yield {
        "current": total,
        "total": total,
        "message": "All chapters processed. Creating zip archive...",
        "filename": ""
    }

    return str(root)

