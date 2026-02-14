"""
Book Converter — Flask routes.
Handles EPUB/PDF upload, conversion streaming, and file download.
"""

import os
import json
import uuid
import shutil
import tempfile
import logging
from flask import render_template, request, send_file, jsonify, Response

from modules.book_converter import bp
from modules.library.routes import save_to_library
from modules.book_converter.converter import (
    parse_epub,
    render_book,
    build_book_xtc,
    convert_pdf_to_epub,
)

logger = logging.getLogger(__name__)

CALIBRE_IO_PATH = os.environ.get("CALIBRE_IO_PATH", "/calibre-io")
SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "x4_book_sessions")

# Ensure sessions directory exists
os.makedirs(SESSIONS_DIR, exist_ok=True)


@bp.route("/")
def index():
    logger.info("Book Converter index page accessed")
    return render_template("book_converter/index.html")





def _stream_epub_to_xtc(epub_path, work_dir, settings, session_id, original_filename=None):
    """Generator that yields NDJSON progress for EPUB→XTC conversion."""
    try:
        yield json.dumps({"type": "progress", "message": "Parsing EPUB...", "current": 0, "total": 100}) + "\n"

        parsed = parse_epub(epub_path)

        for event in render_book(parsed, settings):
            if event["type"] == "progress":
                yield json.dumps(event) + "\n"
            elif event["type"] == "result":
                pages = event["pages"]
                metadata = event["metadata"]
                chapters = event["chapters"]

                yield json.dumps({"type": "progress", "message": "Building XTC file...", "current": 99, "total": 100}) + "\n"

                base = os.path.splitext(original_filename or os.path.basename(epub_path))[0]
                out_filename = f"{base}.xtc"
                out_path = os.path.join(work_dir, out_filename)
                
                size = (settings.get("target_width", 480), settings.get("target_height", 800))
                build_book_xtc(pages, out_path, metadata, chapters, size)

                try:
                    final_path = save_to_library(out_path, out_filename)
                    final_filename = os.path.basename(final_path)
                except Exception:
                    logger.exception("Failed to save XTC to library")
                    final_filename = out_filename  # Fallback (though download will fail)

                yield json.dumps({
                    "type": "done",
                    "download_url": f"/library/download/{final_filename}",
                    "filename": final_filename,
                }) + "\n"

                # Cleanup session immediately
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    logger.warning(f"Failed to cleanup session {session_id}", exc_info=True)

    except Exception as e:
        logger.exception("Conversion error")
        yield json.dumps({"type": "error", "message": str(e)}) + "\n"


@bp.route("/convert", methods=["POST"])
def convert():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    original_filename = file.filename
    filename = original_filename.lower()
    output_format = request.form.get("output_format", "xtc").lower()

    settings = {
        "target_width": int(request.form.get("target_width", "480")),
        "target_height": int(request.form.get("target_height", "800")),
        "font_size": int(request.form.get("font_size", "28")),
        "margin_top": int(request.form.get("margin_top", "30")),
        "margin_bottom": int(request.form.get("margin_bottom", "20")),
        "margin_left": int(request.form.get("margin_left", "20")),
        "margin_right": int(request.form.get("margin_right", "20")),
        "line_height": float(request.form.get("line_height", "1.4")),
        "dithering": request.form.get("dithering", "true").lower() == "true",
        "contrast": float(request.form.get("contrast", "1.2")),
        "text_align": request.form.get("text_align", "justify"),
        "bold": request.form.get("bold", "false").lower() == "true",
        "paragraph_indent": int(request.form.get("paragraph_indent", "0")),
        "paragraph_spacing": float(request.form.get("paragraph_spacing", "0.5")),
    }

    # Session ID is critical for retrieval
    session_id = str(uuid.uuid4())
    
    # Store work files in session directory
    # Facilitates cleanup
    session_dir = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    work_dir = session_dir

    logger.info(f"Convert request: {file.filename}, format={output_format}, session={session_id}")

    try:
        input_path = os.path.join(work_dir, file.filename)
        file.save(input_path)

        if filename.endswith(".pdf"):
            if output_format == "epub":
                # PDF → EPUB via Calibre
                try:
                    epub_path = convert_pdf_to_epub(input_path, CALIBRE_IO_PATH)
                    epub_filename = os.path.splitext(original_filename)[0] + ".epub"

                    # Copy to work_dir for serving
                    local_epub = os.path.join(work_dir, epub_filename)
                    shutil.copy2(epub_path, local_epub)

                    try:
                        final_path = save_to_library(local_epub, epub_filename)
                        final_filename = os.path.basename(final_path)
                    except Exception:
                        logger.exception("Failed to save EPUB to library")
                        final_filename = epub_filename

                    shutil.rmtree(work_dir, ignore_errors=True)

                    return jsonify({
                        "type": "done",
                        "download_url": f"/library/download/{final_filename}",
                        "filename": final_filename,
                    })
                except (TimeoutError, FileNotFoundError) as e:
                    # Clean up on error
                    shutil.rmtree(work_dir, ignore_errors=True)
                    return jsonify({"error": str(e)}), 503

            else:
                # PDF → XTC (Calibre first, then render)
                def stream_pdf_to_xtc():
                    try:
                        yield json.dumps({"type": "progress", "message": "Converting PDF to EPUB via Calibre...", "current": 0, "total": 100}) + "\n"
                        epub_path = convert_pdf_to_epub(input_path, CALIBRE_IO_PATH)
                        yield from _stream_epub_to_xtc(epub_path, work_dir, settings, session_id, original_filename)
                    except (TimeoutError, FileNotFoundError) as e:
                        yield json.dumps({"type": "error", "message": str(e)}) + "\n"
                    except Exception as e:
                        logger.exception("PDF→XTC error")
                        yield json.dumps({"type": "error", "message": str(e)}) + "\n"
                    finally:
                        if os.path.exists(work_dir):
                             shutil.rmtree(work_dir, ignore_errors=True)

                return Response(stream_pdf_to_xtc(), mimetype="application/x-ndjson")

        elif filename.endswith(".epub"):
            if output_format == "xtc":
                # EPUB → XTC
                return Response(
                    _stream_epub_to_xtc(input_path, work_dir, settings, session_id, original_filename),
                    mimetype="application/x-ndjson",
                )
            else:
                shutil.rmtree(work_dir, ignore_errors=True)
                return jsonify({"error": "EPUB is already in EPUB format"}), 400
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": "Unsupported file type. Upload a PDF or EPUB file."}), 400

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.exception("Convert route error")
        return jsonify({"error": str(e)}), 500



