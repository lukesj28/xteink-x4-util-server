"""
Book Converter — Core engine.
Parses EPUB files, renders pages via PyMuPDF, and builds XTC binary files.
Supports PDF→EPUB conversion via a shared Calibre volume.
"""

import os
import re
import shutil
import struct
import time
import base64
import hashlib
import tempfile
import logging
from urllib.parse import unquote

import fitz
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from PIL import Image, ImageEnhance

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "target_width": 480,
    "target_height": 800,
    "font_size": 28,
    "margin_top": 30,
    "margin_bottom": 20,
    "margin_left": 20,
    "margin_right": 20,
    "line_height": 1.4,
    "dithering": True,
    "contrast": 1.2,
    "text_align": "justify",
    "bold": False,
    "paragraph_indent": 0,
    "paragraph_spacing": 0.5,
}


def _merge_settings(raw):
    s = dict(DEFAULT_SETTINGS)
    if raw:
        for key in DEFAULT_SETTINGS:
            if key in raw:
                if key in ("dithering", "bold"):
                    s[key] = bool(raw[key])
                elif key in ("target_width", "target_height", "font_size",
                             "margin_top", "margin_bottom", "margin_left", "margin_right",
                             "paragraph_indent"):
                    s[key] = int(raw[key])
                elif key == "text_align":
                    s[key] = str(raw[key])
                else:
                    s[key] = float(raw[key])
    return s


# ------------------------------------------------------------------
# EPUB Parsing
# ------------------------------------------------------------------

def _extract_images_to_base64(book):
    image_map = {}
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            filename = os.path.basename(item.get_name())
            b64_data = base64.b64encode(item.get_content()).decode("utf-8")
            image_map[filename] = f"data:{item.media_type};base64,{b64_data}"
        except Exception:
            pass
    return image_map


def _extract_all_css(book):
    css_rules = []
    for item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        try:
            css_rules.append(item.get_content().decode("utf-8", errors="ignore"))
        except Exception:
            pass
    return "\n".join(css_rules)


def _get_toc_mapping(book):
    mapping = {}

    def add_entry(href, title):
        if "#" in href:
            href_clean, anchor = href.split("#", 1)
        else:
            href_clean, anchor = href, None
        filename = os.path.basename(href_clean)
        if filename not in mapping:
            mapping[filename] = []
        mapping[filename].append((anchor, title))

    def process_toc_item(item):
        if isinstance(item, tuple):
            if len(item) > 1 and isinstance(item[1], list):
                for sub in item[1]:
                    process_toc_item(sub)
        elif isinstance(item, epub.Link):
            add_entry(item.href, item.title)

    for item in book.toc:
        process_toc_item(item)

    if not mapping:
        nav_item = next(
            (i for i in book.get_items() if i.get_type() == ebooklib.ITEM_NAVIGATION),
            None,
        )
        if nav_item:
            try:
                soup = BeautifulSoup(nav_item.get_content(), "html.parser")
                nav_el = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
                if nav_el:
                    for link in nav_el.find_all("a", href=True):
                        add_entry(link["href"], link.get_text().strip())
            except Exception:
                pass

    return mapping


def parse_epub(epub_path):
    """
    Parse an EPUB file and return structured data.

    Returns dict: {title, author, lang, chapters: [{title, body_html, has_image}],
                   images: {basename: data_uri}, css}
    """
    book = epub.read_epub(epub_path)

    # Metadata
    titles = book.get_metadata("DC", "title")
    title = titles[0][0] if titles else "Unknown Title"
    authors = book.get_metadata("DC", "creator")
    author = authors[0][0] if authors else "Unknown Author"
    langs = book.get_metadata("DC", "language")
    lang = langs[0][0] if langs else "en"

    images = _extract_images_to_base64(book)
    css = _extract_all_css(book)
    toc_mapping = _get_toc_mapping(book)

    spine_items = [
        book.get_item_with_id(ref[0])
        for ref in book.spine
        if isinstance(book.get_item_with_id(ref[0]), epub.EpubHtml)
    ]

    chapters = []
    for item in spine_items:
        item_filename = os.path.basename(item.get_name())
        raw_html = item.get_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(raw_html, "html.parser")
        has_image = bool(soup.find("img"))

        toc_entries = toc_mapping.get(item_filename)
        if toc_entries:
            chapter_title = toc_entries[0][1]
        else:
            chapter_title = None
            for tag in ("h1", "h2", "h3"):
                header = soup.find(tag)
                if header:
                    t = header.get_text().strip()
                    if t and len(t) < 150:
                        chapter_title = t
                        break
            if not chapter_title:
                chapter_title = f"Section {len(chapters) + 1}"

        body_html = (
            "".join(str(x) for x in soup.body.contents) if soup.body else str(soup)
        )
        chapters.append(
            {
                "title": chapter_title,
                "body_html": body_html,
                "has_image": has_image,
            }
        )

    return {
        "title": title,
        "author": author,
        "lang": lang,
        "chapters": chapters,
        "images": images,
        "css": css,
    }


# ------------------------------------------------------------------
# Page Rendering (PyMuPDF pipeline)
# ------------------------------------------------------------------

def render_book(parsed, settings=None):
    """
    Render a parsed EPUB to a list of processed PIL images.

    Yields progress dicts during rendering, then yields a final result dict:
        {"type": "result", "pages": [...], "metadata": {...}, "chapters": [...]}
    """
    s = _merge_settings(settings)
    w = s["target_width"]
    h = s["target_height"]
    font_size = s["font_size"]
    margin_top = s["margin_top"]
    margin_bottom = s["margin_bottom"]
    margin_left = s["margin_left"]
    margin_right = s["margin_right"]
    line_height = s["line_height"]
    dithering = s["dithering"]
    contrast = s["contrast"]
    text_align = s["text_align"]
    bold = s["bold"]
    paragraph_indent = s["paragraph_indent"]
    paragraph_spacing = s["paragraph_spacing"]

    epub_css = parsed["css"]
    images = parsed["images"]
    chapters_data = parsed["chapters"]

    font_weight = "bold" if bold else "normal"

    custom_css = f"""
        @page {{ size: {w}pt {h}pt; margin: {margin_top}px {margin_right}px {margin_bottom}px {margin_left}px; }}
        body {{
            font-family: serif !important;
            font-size: {font_size}pt !important;
            font-weight: {font_weight} !important;
            line-height: {line_height} !important;
            text-align: {text_align} !important;
            color: black !important;
            margin: 0 !important;
            padding: 0 !important;
            background-color: white !important;
            width: 100% !important;
            height: 100% !important;
            overflow-wrap: break-word;
        }}
        p, div, li, blockquote, dd, dt {{
            font-family: inherit !important;
            font-size: inherit !important;
            font-weight: inherit !important;
            line-height: inherit !important;
            text-align: inherit !important;
            color: inherit !important;
            text-indent: {paragraph_indent}px !important;
            margin-bottom: {paragraph_spacing}em !important;
        }}
        img {{ max-width: 95% !important; height: auto !important; display: block; margin: 20px auto !important; }}
        h1, h2, h3 {{ text-align: center !important; margin-top: 1em; font-weight: bold !important; }}
    """

    total_chapters = len(chapters_data)
    all_pages = []
    chapter_records = []  # (title, start_page, end_page)
    render_dir = tempfile.mkdtemp(prefix="book_render_")

    try:
        for ch_idx, chapter in enumerate(chapters_data):
            yield {
                "type": "progress",
                "message": f"Rendering chapter {ch_idx + 1}/{total_chapters}: {chapter['title']}",
                "current": ch_idx + 1,
                "total": total_chapters,
            }

            body_html = chapter["body_html"]

            # Replace image sources with base64 data URIs
            soup = BeautifulSoup(body_html, "html.parser")
            for img_tag in soup.find_all("img"):
                src = img_tag.get("src", "")
                basename = os.path.basename(unquote(src))
                if basename in images:
                    img_tag["src"] = images[basename]
            body_html = str(soup)

            full_html = (
                f"<html><head>"
                f"<style>{epub_css}</style>"
                f"<style>{custom_css}</style>"
                f"</head><body>{body_html}</body></html>"
            )

            temp_html = os.path.join(render_dir, f"ch_{ch_idx}.html")
            with open(temp_html, "w", encoding="utf-8") as f:
                f.write(full_html)

            doc = fitz.open(temp_html)
            doc.layout(rect=fitz.Rect(0, 0, w, h))

            start_page = len(all_pages)

            for page_idx in range(len(doc)):
                page = doc[page_idx]
                sx = w / page.rect.width
                sy = h / page.rect.height
                mat = fitz.Matrix(sx, sy)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                img = img.convert("L")

                # Apply contrast
                if contrast != 1.0:
                    img = ImageEnhance.Contrast(img).enhance(contrast)

                # Apply dithering
                if dithering:
                    img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")

                all_pages.append(img)

            doc.close()

            end_page = len(all_pages) - 1
            chapter_records.append((chapter["title"], start_page, end_page))

    finally:
        shutil.rmtree(render_dir, ignore_errors=True)

    yield {
        "type": "result",
        "pages": all_pages,
        "metadata": {
            "title": parsed["title"],
            "author": parsed["author"],
            "lang": parsed["lang"],
        },
        "chapters": chapter_records,
    }


# ------------------------------------------------------------------
# XTC Building (book format with metadata + chapters)
# ------------------------------------------------------------------

def _pack_metadata(title, author, lang, chapter_count):
    """Pack metadata into a 256-byte fixed-size block."""
    blob = bytearray(256)
    struct.pack_into("<128s", blob, 0x00, title.encode("utf-8")[:127])
    struct.pack_into("<64s", blob, 0x80, author.encode("utf-8")[:63])
    struct.pack_into("<32s", blob, 0xC0, b"X4Utils")
    struct.pack_into("<16s", blob, 0xE0, lang.encode("utf-8")[:15])
    struct.pack_into("<I", blob, 0xF0, int(time.time()))
    struct.pack_into("<H", blob, 0xF4, 0)  # cover page
    struct.pack_into("<H", blob, 0xF6, chapter_count)
    return bytes(blob)


def _pack_chapter(name, start_pg, end_pg):
    """Pack a single chapter into a 96-byte fixed-size block."""
    blob = bytearray(96)
    struct.pack_into("<80s", blob, 0x00, name.encode("utf-8")[:79])
    struct.pack_into("<H", blob, 0x50, start_pg)
    struct.pack_into("<H", blob, 0x52, end_pg)
    return bytes(blob)


def _image_to_xtg_blob(img, w, h):
    """Convert a grayscale PIL image to an XTG page blob (header + 1-bit bitmap)."""
    if img.size != (w, h):
        img = img.resize((w, h), Image.LANCZOS)

    img_1bit = img.convert("1")
    bitmap_data = img_1bit.tobytes()
    data_size = ((w + 7) // 8) * h

    xt_header = struct.pack("<IHHBBIQ", 0x00475458, w, h, 0, 0, data_size, 0)
    return xt_header + bitmap_data


def build_book_xtc(pages, out_path, metadata, chapters, size):
    """
    Build a book XTC file with metadata and chapter records.

    pages: list of grayscale PIL images
    metadata: dict with title, author, lang
    chapters: list of (title, start_page, end_page) tuples
    size: (width, height) tuple
    """
    w, h = size
    total_pages = len(pages)
    num_chaps = len(chapters)

    metadata_off = 56
    chapter_off = metadata_off + 256
    index_off = chapter_off + (num_chaps * 96)
    data_off = index_off + (total_pages * 16)

    # Metadata block
    metadata_block = _pack_metadata(
        metadata["title"], metadata["author"], metadata["lang"], num_chaps
    )

    # Chapter blocks
    chapter_block = bytearray()
    for title, start_pg, end_pg in chapters:
        chapter_block.extend(_pack_chapter(title, start_pg, end_pg))

    # Process pages → XTG blobs
    blob_data = bytearray()
    index_table = bytearray()

    for img in pages:
        page_blob = _image_to_xtg_blob(img, w, h)
        index_table.extend(
            struct.pack("<QIHH", data_off + len(blob_data), len(page_blob), w, h)
        )
        blob_data.extend(page_blob)

    # XTC header (56 bytes)
    header = struct.pack(
        "<IHHBBBBIQQQQQ",
        0x00435458,   # "XTC\0"
        0x0100,       # version
        total_pages,  # page count
        0,            # readDirection
        1,            # hasMetadata
        0,            # hasThumbnails
        1,            # hasChapters
        1,            # currentPage
        metadata_off,
        index_off,
        data_off,
        0,            # thumbOffset
        chapter_off,
    )

    with open(out_path, "wb") as f:
        f.write(header)
        f.write(metadata_block)
        f.write(chapter_block)
        f.write(index_table)
        f.write(blob_data)

    logger.info(f"XTC written: {out_path} ({total_pages} pages, {num_chaps} chapters)")


# ------------------------------------------------------------------
# PDF → EPUB via shared Calibre volume
# ------------------------------------------------------------------

def convert_pdf_to_epub(pdf_path, calibre_io_path, poll_interval=2, timeout=120):
    """
    Convert a PDF to EPUB by placing it in the Calibre shared volume.

    Copies PDF to {calibre_io_path}/input/, then polls {calibre_io_path}/output/
    for the resulting .epub file.

    Returns the path to the converted EPUB, or raises an error.
    """
    input_dir = os.path.join(calibre_io_path, "input")
    output_dir = os.path.join(calibre_io_path, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    pdf_name = os.path.basename(pdf_path)
    epub_name = os.path.splitext(pdf_name)[0] + ".epub"
    dest = os.path.join(input_dir, pdf_name)

    shutil.copy2(pdf_path, dest)
    logger.info(f"PDF copied to Calibre input: {dest}")

    elapsed = 0
    epub_out = os.path.join(output_dir, epub_name)
    while elapsed < timeout:
        if os.path.exists(epub_out) and os.path.getsize(epub_out) > 0:
            logger.info(f"Calibre conversion complete: {epub_out}")
            # Clean up input
            try:
                os.remove(dest)
            except OSError:
                pass
            return epub_out
        time.sleep(poll_interval)
        elapsed += poll_interval

    # Clean up input on timeout
    try:
        os.remove(dest)
    except OSError:
        pass
    raise TimeoutError(
        f"Calibre did not produce {epub_name} within {timeout}s. "
        "Ensure the Calibre container is running and configured."
    )
