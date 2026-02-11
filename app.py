"""
Manga Formatter — Flask web application.
Upload CBZ files or scan a host directory, configure settings,
download structured XTC output as zip.
"""

import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from flask import Flask, request, render_template, send_file, jsonify, Response

from converter import (
    classify_cbz_files,
    convert_chapters,
    get_cbz_preview,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload limit

# In-memory session store: session_id -> session data dict
_sessions = {}


@app.route("/")
def index():
    return render_template("index.html")


BROWSE_ROOT = "/mangas"


def _format_size(size_bytes):
    """Format bytes into human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@app.route("/browse", methods=["GET"])
def browse_directory():
    """Browse directories and files under /mangas."""
    raw_path = request.args.get("path", BROWSE_ROOT).strip()

    # Resolve and enforce that path stays within BROWSE_ROOT
    resolved = os.path.realpath(raw_path)
    root_resolved = os.path.realpath(BROWSE_ROOT)

    if not resolved.startswith(root_resolved):
        return jsonify({"error": "Access denied: path outside allowed root"}), 403

    if not os.path.isdir(resolved):
        return jsonify({"error": f"Directory not found: {raw_path}"}), 404

    dirs = []
    files = []

    try:
        for entry in sorted(os.scandir(resolved), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                dirs.append({"name": entry.name, "path": os.path.join(resolved, entry.name)})
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat()
                files.append({"name": entry.name, "size": _format_size(stat.st_size)})
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    # Parent path (only if not at root)
    parent = None
    if resolved != root_resolved:
        parent = os.path.dirname(resolved)

    return jsonify({
        "current_path": resolved,
        "parent": parent,
        "dirs": dirs,
        "files": files,
    })


@app.route("/convert", methods=["POST"])
def convert():
    manga_title = request.form.get("title", "").strip()
    if not manga_title:
        return jsonify({"error": "Manga title is required"}), 400

    # Parse settings from form
    settings = {
        "dithering": request.form.get("dithering", "true").lower() == "true",
        "contrast": int(request.form.get("contrast", "4")),
        "target_width": int(request.form.get("target_width", "480")),
        "target_height": int(request.form.get("target_height", "800")),
    }

    source_mode = request.form.get("source_mode", "upload")
    work_dir = tempfile.mkdtemp(prefix="manga_fmt_")

    try:
        cbz_paths = []

        if source_mode == "hostdir":
            # Read from host directory
            host_path = request.form.get("host_path", "").strip()
            if not host_path or not os.path.isdir(host_path):
                return jsonify({"error": f"Invalid host directory: {host_path}"}), 400
            for f in sorted(os.listdir(host_path)):
                if f.lower().endswith(".cbz"):
                    cbz_paths.append(os.path.join(host_path, f))
        else:
            # Upload mode
            files = request.files.getlist("cbz_files")
            if not files or all(f.filename == "" for f in files):
                return jsonify({"error": "No CBZ files uploaded"}), 400

            cbz_dir = os.path.join(work_dir, "input")
            os.makedirs(cbz_dir, exist_ok=True)

            for f in files:
                if f.filename and f.filename.lower().endswith(".cbz"):
                    save_path = os.path.join(cbz_dir, f.filename)
                    f.save(save_path)
                    cbz_paths.append(save_path)

        if not cbz_paths:
            return jsonify({"error": "No valid .cbz files found"}), 400

        # Classify files
        recognized, unrecognized = classify_cbz_files(cbz_paths)

        # Set up output
        output_base = os.path.join(work_dir, "output")
        os.makedirs(output_base, exist_ok=True)

        # Convert recognized files
        if recognized:
            convert_chapters(recognized, output_base, manga_title, settings)

        if unrecognized:
            # Two-phase: return review data
            session_id = str(uuid.uuid4())
            _sessions[session_id] = {
                "work_dir": work_dir,
                "output_base": output_base,
                "manga_title": manga_title,
                "settings": settings,
                "recognized": recognized,
                "unrecognized": {os.path.basename(p): p for p in unrecognized},
            }

            unrecognized_info = []
            for path in unrecognized:
                basename = os.path.basename(path)
                unrecognized_info.append({
                    "filename": basename,
                    "preview_url": f"/preview/{session_id}/{basename}",
                })

            return jsonify({
                "status": "needs_review",
                "session_id": session_id,
                "recognized_count": len(recognized),
                "recognized_chapters": sorted(recognized.keys()),
                "unrecognized": unrecognized_info,
            })

        # All recognized — zip and return directly
        result_dir = os.path.join(output_base, manga_title)
        zip_path = os.path.join(work_dir, f"{manga_title}.zip")
        _zip_directory(result_dir, zip_path, manga_title)

        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{manga_title}.zip",
        )

    except Exception as e:
        # Clean up on error (only if no session was created)
        if work_dir and not any(s.get("work_dir") == work_dir for s in _sessions.values()):
            shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": str(e)}), 500


@app.route("/preview/<session_id>/<filename>", methods=["GET"])
def preview(session_id, filename):
    """Return JPEG thumbnail of the first page of a CBZ."""
    session = _sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    cbz_path = session["unrecognized"].get(filename)
    if not cbz_path:
        return jsonify({"error": "File not found in session"}), 404

    jpeg_bytes = get_cbz_preview(cbz_path)
    if not jpeg_bytes:
        return jsonify({"error": "Could not extract preview"}), 500

    return Response(jpeg_bytes, mimetype="image/jpeg")


@app.route("/convert/continue", methods=["POST"])
def convert_continue():
    """Phase 2: accept user chapter assignments for unrecognized files."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON data"}), 400

    session_id = data.get("session_id")
    assignments = data.get("assignments", {})

    session = _sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found or expired"}), 404

    try:
        work_dir = session["work_dir"]
        output_base = session["output_base"]
        manga_title = session["manga_title"]
        settings = session["settings"]
        recognized = session["recognized"]

        # Build chapter map from user assignments
        manual_map = {}
        for filename, ch_num_str in assignments.items():
            ch_num = int(ch_num_str)
            cbz_path = session["unrecognized"].get(filename)
            if cbz_path and ch_num not in recognized:
                manual_map[ch_num] = cbz_path

        # Convert the manually assigned files
        if manual_map:
            convert_chapters(manual_map, output_base, manga_title, settings)

        # Zip and return
        result_dir = os.path.join(output_base, manga_title)
        zip_path = os.path.join(work_dir, f"{manga_title}.zip")
        _zip_directory(result_dir, zip_path, manga_title)

        # Clean up session
        del _sessions[session_id]

        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{manga_title}.zip",
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _zip_directory(source_dir, zip_path, root_name):
    """Create a zip with root_name as the top-level folder."""
    source = Path(source_dir)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source.rglob("*")):
            if file_path.is_file():
                arcname = os.path.join(root_name, file_path.relative_to(source))
                zf.write(file_path, arcname)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
