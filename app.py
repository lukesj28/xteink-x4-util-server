"""
Manga Formatter — Flask web application.
Upload CBZ files or scan a host directory, configure settings,
download structured XTC output as zip.
"""

import os
import shutil
import tempfile
import json
import time

# ... (imports) ...


@app.route("/download/<session_id>", methods=["GET"])
def download_zip(session_id):
    """Serve the generated zip file for a session."""
    session = _sessions.get(session_id)
    if not session or "zip_path" not in session:
        return jsonify({"error": "File not found or expired"}), 404
    
    return send_file(
        session["zip_path"],
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{session['manga_title']}.zip",
    )


def _stream_conversion(chapter_map, work_dir, output_base, manga_title, settings, session_id):
    """Generator that yields NDJSON progress updates."""
    try:
        # 1. Convert
        converter_gen = convert_chapters(chapter_map, output_base, manga_title, settings)
        root_out = None
        
        for progress in converter_gen:
            # If it's a progress dict
            if isinstance(progress, dict):
                yield json.dumps({"type": "progress", **progress}) + "\n"
            else:
                # The generator returns the root path at the end? 
                # No, generator returns strictly what is yielded. 
                # The 'return' value of a generator is caught by StopIteration, 
                # but we can't easily access it in a for-loop.
                # So we modified convert_chapters to NOT return, but we need the path.
                # Wait, convert_chapters yields progress, then ends.
                # It calculates 'root' internally. 
                # We can reconstruct it:
                pass
        
        root_out = os.path.join(output_base, manga_title)

        # 2. Zip
        yield json.dumps({"type": "progress", "message": "Zipping files...", "current": 100, "total": 100}) + "\n"
        
        zip_path = os.path.join(work_dir, f"{manga_title}.zip")
        _zip_directory(root_out, zip_path, manga_title)
        
        # Store for download
        if session_id not in _sessions:
            _sessions[session_id] = {}
        _sessions[session_id]["zip_path"] = zip_path
        _sessions[session_id]["manga_title"] = manga_title
        # Keep work_dir to prevent cleanup if needed? 
        # Actually _sessions might already have it if this was a continue.
        
        # 3. Done
        yield json.dumps({
            "type": "done", 
            "download_url": f"/download/{session_id}"
        }) + "\n"

    except Exception as e:
        yield json.dumps({"type": "error", "message": str(e)}) + "\n"


@app.route("/convert", methods=["POST"])
def convert():
    manga_title = request.form.get("title", "").strip()
    if not manga_title:
        return jsonify({"error": "Manga title is required"}), 400

    settings = {
        "dithering": request.form.get("dithering", "true").lower() == "true",
        "contrast": int(request.form.get("contrast", "4")),
        "target_width": int(request.form.get("target_width", "480")),
        "target_height": int(request.form.get("target_height", "800")),
    }

    source_mode = request.form.get("source_mode", "upload")
    work_dir = tempfile.mkdtemp(prefix="manga_fmt_")
    session_id = str(uuid.uuid4())

    try:
        cbz_paths = []
        if source_mode == "hostdir":
            host_path = request.form.get("host_path", "").strip()
            if not host_path or not os.path.isdir(host_path):
                return jsonify({"error": f"Invalid host directory: {host_path}"}), 400
            for f in sorted(os.listdir(host_path)):
                if f.lower().endswith(".cbz"):
                    cbz_paths.append(os.path.join(host_path, f))
        else:
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

        recognized, unrecognized = classify_cbz_files(cbz_paths)
        output_base = os.path.join(work_dir, "output")
        os.makedirs(output_base, exist_ok=True)

        # If unrecognized files exist -> Return JSON (Phase 2 trigger)
        # We can't stream this part easily if we also want to stream the "recognized" part 
        # unless we do everything in one stream.
        # BUT the frontend expects "needs_review" JSON to switch UI.
        # So: if unrecognized, return JSON immediately.
        
        if unrecognized:
            _sessions[session_id] = {
                "work_dir": work_dir,
                "output_base": output_base,
                "manga_title": manga_title,
                "settings": settings,
                "recognized": recognized, # Stored to be converted later
                "unrecognized": {os.path.basename(p): p for p in unrecognized},
            }
            
            unrecognized_info = [
                {"filename": os.path.basename(p), "preview_url": f"/preview/{session_id}/{os.path.basename(p)}"}
                for p in unrecognized
            ]

            return jsonify({
                "status": "needs_review",
                "session_id": session_id,
                "recognized_count": len(recognized),
                "recognized_chapters": sorted(recognized.keys()),
                "unrecognized": unrecognized_info,
            })

        # If NO unrecognized -> Stream conversion of recognized
        _sessions[session_id] = {"work_dir": work_dir} # Track for cleanup
        return Response(
            _stream_conversion(recognized, work_dir, output_base, manga_title, settings, session_id),
            mimetype="application/x-ndjson"
        )

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": str(e)}), 500


@app.route("/convert/continue", methods=["POST"])
def convert_continue():
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

        # Merge assignments
        final_map = recognized.copy()
        for filename, ch_num_str in assignments.items():
            ch_num = int(ch_num_str)
            cbz_path = session["unrecognized"].get(filename)
            if cbz_path:
                final_map[ch_num] = cbz_path

        return Response(
            _stream_conversion(final_map, work_dir, output_base, manga_title, settings, session_id),
            mimetype="application/x-ndjson"
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
