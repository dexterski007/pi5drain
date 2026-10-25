#!/usr/bin/env python3
"""
web_app.py
----------
Web interface for Pixeldrain Uploader designed for Raspberry Pi and home servers.
Provides isolated access to a single designated folder, enabling direct browser uploads,
background Pixeldrain uploads, real-time progress tracking, and link history.

Supports:
- Subfolder browsing and creation within the designated folder
- Filenames with spaces and unicode
- Mobile-responsive web interface
- Automatic link copying to clipboard
- Strict path-traversal protection
"""

import argparse
import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request

# Import uploader engine from pixeldrain_uploader.py
from pixeldrain_uploader import (
    DEFAULT_LOG_FILE,
    ErrorCode,
    PixeldrainUploadError,
    PixeldrainUploader,
    format_bytes,
    load_env_file,
    setup_logging,
)

logger = logging.getLogger("pixeldrain")

# Default paths
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "settings.json" if (BASE_DIR / "settings.json").exists() else BASE_DIR / "config.json"
HISTORY_FILE = BASE_DIR / "upload_history.json"
DEFAULT_STORAGE_FOLDER = BASE_DIR / "storage"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # No arbitrary Flask limit on incoming file uploads

# Global state
ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
WATCHED_FOLDER = DEFAULT_STORAGE_FOLDER


# ============================================================================
# CONFIG & HISTORY HELPERS
# ============================================================================

def load_config() -> Dict[str, Any]:
    config = {
        "api_key": "",
        "folder": str(DEFAULT_STORAGE_FOLDER),
        "logging_enabled": False,
        "auto_upload_on_add": False,
        "auto_copy_link": True
    }

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                config.update(saved)
        except Exception:
            pass

    if not config.get("api_key"):
        load_env_file()
        config["api_key"] = os.environ.get("PIXELDRAIN_API_KEY", "")

    return config


def save_config(config: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error("Failed to save config.json: %s", e)


def load_history() -> Dict[str, Dict[str, Any]]:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_history(history: Dict[str, Dict[str, Any]]) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error("Failed to save upload_history.json: %s", e)


def sanitize_name(name: str) -> Optional[str]:
    """
    Sanitize a single filename or folder name component.
    Preserves spaces and unicode, strips forbidden characters.
    Does NOT allow path separators or traversal.
    """
    if not name:
        return None

    cleaned = name.strip().replace("\x00", "")
    base = os.path.basename(cleaned)

    if not base or base in (".", "..") or "/" in cleaned or "\\" in cleaned:
        return None

    illegal_chars = '<>:"/\\|?*'
    safe = "".join(c for c in base if c not in illegal_chars).strip()
    return safe if safe and safe not in (".", "..") else None


def resolve_subpath(subpath: str) -> Optional[Path]:
    """
    Resolve a relative subpath under WATCHED_FOLDER.
    Returns the resolved absolute path if safe, None if traversal is detected.
    The subpath can be '' or '.' for root, or 'sub/folder/path' for nested.
    """
    watched_resolved = WATCHED_FOLDER.resolve()

    if not subpath or subpath in (".", "/", "\\"):
        return watched_resolved

    # Normalize separators and reject null bytes
    normalized = subpath.replace("\\", "/").replace("\x00", "").strip("/")
    if not normalized or normalized == ".":
        return watched_resolved

    # Check each component for traversal
    parts = normalized.split("/")
    for part in parts:
        if part in (".", "..") or not part:
            return None

    target = (watched_resolved / normalized).resolve()

    try:
        target.relative_to(watched_resolved)
    except ValueError:
        return None

    return target


def get_safe_file_path(filename: str, subpath: str = "") -> Optional[Path]:
    """
    Build a safe absolute path for a file inside WATCHED_FOLDER (optionally inside a subpath).
    Preserves spaces and valid characters while preventing path traversal.
    """
    safe_name = sanitize_name(filename)
    if not safe_name:
        return None

    parent = resolve_subpath(subpath)
    if parent is None:
        return None

    target_path = (parent / safe_name).resolve()

    # Ensure the result is still within the watched folder
    try:
        target_path.relative_to(WATCHED_FOLDER.resolve())
    except ValueError:
        return None

    return target_path


def get_relative_path(absolute_path: Path) -> str:
    """Return the path of a file/folder relative to WATCHED_FOLDER, using forward slashes."""
    try:
        return str(absolute_path.resolve().relative_to(WATCHED_FOLDER.resolve())).replace("\\", "/")
    except ValueError:
        return ""


def get_disk_info(path: Path) -> Dict[str, str]:
    """Get disk usage for the folder partition."""
    try:
        total, used, free = shutil.disk_usage(path)
        return {
            "total": format_bytes(total),
            "used": format_bytes(used),
            "free": format_bytes(free),
            "percent_used": f"{(used / total) * 100:.1f}%"
        }
    except Exception:
        return {"total": "N/A", "used": "N/A", "free": "N/A", "percent_used": "0%"}


# ============================================================================
# BACKGROUND UPLOAD WORKER
# ============================================================================

def background_upload_worker(job_id: str, file_path: Path, api_key: str):
    uploader = PixeldrainUploader(api_key=api_key)
    cancel_event = threading.Event()

    with JOBS_LOCK:
        ACTIVE_JOBS[job_id]["cancel_event"] = cancel_event

    total_size = file_path.stat().st_size if file_path.exists() else 0
    last_time = time.time()
    last_bytes = 0

    def progress_callback(chunk_delta: int, total_sent: int, size: int):
        nonlocal last_time, last_bytes
        now = time.time()
        elapsed = now - last_time

        with JOBS_LOCK:
            job = ACTIVE_JOBS.get(job_id)
            if not job:
                return

            job["bytes_uploaded"] = total_sent
            job["total_bytes"] = size
            job["percent"] = min(int((total_sent / size) * 100), 100) if size > 0 else 0

            if elapsed >= 0.4:
                speed = (total_sent - last_bytes) / elapsed if elapsed > 0 else 0
                last_bytes = total_sent
                last_time = now
                job["speed_str"] = f"{format_bytes(speed)}/s"

                remaining = max(0, size - total_sent)
                if speed > 0:
                    secs = int(remaining / speed)
                    m, s = divmod(secs, 60)
                    h, m = divmod(m, 60)
                    job["eta_str"] = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    try:
        with JOBS_LOCK:
            ACTIVE_JOBS[job_id]["status"] = "uploading"

        result = uploader.upload(
            file_path=file_path,
            show_progress=False,
            progress_callback=progress_callback,
            cancel_event=cancel_event
        )

        with JOBS_LOCK:
            ACTIVE_JOBS[job_id]["status"] = "completed"
            ACTIVE_JOBS[job_id]["percent"] = 100
            ACTIVE_JOBS[job_id]["result"] = result
            ACTIVE_JOBS[job_id]["speed_str"] = "Done"
            ACTIVE_JOBS[job_id]["eta_str"] = "00:00"

        # Update persistent history (key by relative path from WATCHED_FOLDER)
        rel_path = get_relative_path(file_path)
        history = load_history()
        history[rel_path] = {
            "id": result["id"],
            "name": file_path.name,
            "rel_path": rel_path,
            "size": result["size_bytes"],
            "view_url": result["view_url"],
            "download_url": result["download_url"],
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_history(history)
        logger.info("Web upload finished for '%s' -> %s", rel_path, result["view_url"])

    except PixeldrainUploadError as err:
        with JOBS_LOCK:
            ACTIVE_JOBS[job_id]["status"] = "cancelled" if err.code == ErrorCode.UPLOAD_CANCELLED else "failed"
            ACTIVE_JOBS[job_id]["error_code"] = err.code.code_str
            ACTIVE_JOBS[job_id]["error_number"] = err.code.numeric_code
            ACTIVE_JOBS[job_id]["error_message"] = err.message
            ACTIVE_JOBS[job_id]["error_hint"] = err.code.hint
        logger.error("Web upload failed for '%s': [%s / E%d] %s", file_path.name, err.code.code_str, err.code.numeric_code, err.message)

    except Exception as ex:
        with JOBS_LOCK:
            ACTIVE_JOBS[job_id]["status"] = "failed"
            ACTIVE_JOBS[job_id]["error_code"] = ErrorCode.UNKNOWN_ERROR.code_str
            ACTIVE_JOBS[job_id]["error_number"] = ErrorCode.UNKNOWN_ERROR.numeric_code
            ACTIVE_JOBS[job_id]["error_message"] = str(ex)
            ACTIVE_JOBS[job_id]["error_hint"] = ErrorCode.UNKNOWN_ERROR.hint
        logger.exception("Unexpected error in web upload worker: %s", ex)


# ============================================================================
# HTTP ROUTES
# ============================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    config = load_config()
    disk_info = get_disk_info(WATCHED_FOLDER)
    api_key_set = bool(config.get("api_key", "").strip())
    masked_key = f"{config['api_key'][:4]}...{config['api_key'][-4:]}" if len(config.get("api_key", "")) > 8 else ("Set" if api_key_set else "")

    return jsonify({
        "status": "ok",
        "watched_folder": str(WATCHED_FOLDER.resolve()),
        "api_key_set": api_key_set,
        "masked_api_key": masked_key,
        "logging_enabled": config.get("logging_enabled", False),
        "auto_upload_on_add": config.get("auto_upload_on_add", False),
        "auto_copy_link": config.get("auto_copy_link", True),
        "disk": disk_info
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.get_json() or {}
    config = load_config()

    if "api_key" in data:
        key = str(data["api_key"]).strip()
        if key:
            config["api_key"] = key

    if "logging_enabled" in data:
        config["logging_enabled"] = bool(data["logging_enabled"])
        setup_logging(enabled=config["logging_enabled"])

    if "auto_upload_on_add" in data:
        config["auto_upload_on_add"] = bool(data["auto_upload_on_add"])

    if "auto_copy_link" in data:
        config["auto_copy_link"] = bool(data["auto_copy_link"])

    save_config(config)
    return jsonify({"success": True, "message": "Configuration saved."})


@app.route("/api/files", methods=["GET"])
def api_list_files():
    """List files and subfolders in a directory within the watched folder."""
    subpath = request.args.get("path", "").strip()

    target_dir = resolve_subpath(subpath)
    if target_dir is None:
        return jsonify({"success": False, "error": "Invalid path (traversal blocked)."}), 403

    if not target_dir.exists():
        return jsonify({"success": False, "error": f"Directory does not exist."}), 404

    if not target_dir.is_dir():
        return jsonify({"success": False, "error": "Path is not a directory."}), 400

    history = load_history()
    folders_list = []
    files_list = []
    current_rel = get_relative_path(target_dir) if target_dir != WATCHED_FOLDER.resolve() else ""

    try:
        entries = sorted(target_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))

        with JOBS_LOCK:
            active_by_relpath = {
                job.get("rel_path", job.get("filename", "")): job
                for job in ACTIVE_JOBS.values()
                if job.get("status") in ["queued", "uploading"]
            }

        for entry in entries:
            if entry.name.startswith("."):
                continue

            if entry.is_dir():
                # Count items inside
                try:
                    item_count = sum(1 for e in entry.iterdir() if not e.name.startswith("."))
                except PermissionError:
                    item_count = 0

                folders_list.append({
                    "name": entry.name,
                    "type": "folder",
                    "rel_path": get_relative_path(entry),
                    "item_count": item_count,
                    "modified": datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                })

            elif entry.is_file():
                stat = entry.stat()
                fname = entry.name
                rel_path = get_relative_path(entry)

                file_hist = history.get(rel_path) or history.get(fname)
                active_job = active_by_relpath.get(rel_path)

                if active_job:
                    status = active_job["status"]
                    pixeldrain_info = None
                elif file_hist:
                    status = "uploaded"
                    pixeldrain_info = file_hist
                else:
                    status = "ready"
                    pixeldrain_info = None

                files_list.append({
                    "name": fname,
                    "type": "file",
                    "rel_path": rel_path,
                    "size_bytes": stat.st_size,
                    "size_formatted": format_bytes(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "status": status,
                    "pixeldrain": pixeldrain_info,
                    "active_job_id": active_job.get("id") if active_job else None
                })

    except Exception as e:
        logger.error("Error reading directory '%s': %s", target_dir, e)
        return jsonify({"success": False, "error": str(e)}), 500

    # Build breadcrumb parts
    breadcrumb = [{"name": "Root", "path": ""}]
    if current_rel:
        parts = current_rel.split("/")
        for i, part in enumerate(parts):
            breadcrumb.append({
                "name": part,
                "path": "/".join(parts[:i+1])
            })

    return jsonify({
        "success": True,
        "current_path": current_rel,
        "breadcrumb": breadcrumb,
        "total_folders": len(folders_list),
        "total_files": len(files_list),
        "folders": folders_list,
        "files": files_list
    })


@app.route("/api/folder/create", methods=["POST"])
def api_create_folder():
    """Create a new subfolder inside the watched folder."""
    data = request.get_json() or {}
    folder_name = data.get("name", "").strip()
    parent_path = data.get("path", "").strip()

    safe_name = sanitize_name(folder_name)
    if not safe_name:
        return jsonify({"success": False, "error": "Invalid folder name."}), 400

    parent = resolve_subpath(parent_path)
    if parent is None:
        return jsonify({"success": False, "error": "Invalid parent path."}), 403

    target = (parent / safe_name).resolve()
    try:
        target.relative_to(WATCHED_FOLDER.resolve())
    except ValueError:
        return jsonify({"success": False, "error": "Path traversal detected."}), 403

    if target.exists():
        return jsonify({"success": False, "error": f"'{safe_name}' already exists."}), 409

    try:
        target.mkdir(parents=False, exist_ok=False)
        rel = get_relative_path(target)
        logger.info("Created folder: %s", rel)
        return jsonify({"success": True, "folder": safe_name, "rel_path": rel})
    except Exception as e:
        logger.error("Failed to create folder '%s': %s", safe_name, e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/folder/delete", methods=["POST"])
def api_delete_folder():
    """Delete a subfolder and all its contents from the watched folder."""
    data = request.get_json() or {}
    folder_path = data.get("path", "").strip()

    if not folder_path:
        return jsonify({"success": False, "error": "Cannot delete root folder."}), 400

    target = resolve_subpath(folder_path)
    if target is None:
        return jsonify({"success": False, "error": "Invalid path."}), 403

    if target == WATCHED_FOLDER.resolve():
        return jsonify({"success": False, "error": "Cannot delete the root storage folder."}), 403

    if not target.is_dir():
        return jsonify({"success": False, "error": "Path is not a directory."}), 400

    try:
        shutil.rmtree(target)
        logger.info("Deleted folder: %s", folder_path)
        return jsonify({"success": True, "message": f"Deleted folder '{folder_path}'"})
    except Exception as e:
        logger.error("Failed to delete folder '%s': %s", folder_path, e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/upload-to-folder", methods=["POST"])
def api_upload_to_folder():
    """Upload a file from browser into the watched folder or a subfolder (preserves spaces)."""
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file part in request"}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No selected file"}), 400

    filename = sanitize_name(file.filename)
    if not filename:
        return jsonify({"success": False, "error": "Invalid filename"}), 400

    subpath = request.form.get("path", "").strip()

    target_path = get_safe_file_path(filename, subpath)
    if not target_path:
        return jsonify({"success": False, "error": "Path traversal detected"}), 403

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        file.save(str(target_path))
        file_size = target_path.stat().st_size
        rel_path = get_relative_path(target_path)
        logger.info("Saved incoming file: '%s' (%s) at '%s'", filename, format_bytes(file_size), rel_path)

        config = load_config()
        job_id = None
        auto_upload = request.form.get("auto_upload") == "true" or config.get("auto_upload_on_add", False)
        if auto_upload and config.get("api_key"):
            job_id = str(uuid.uuid4())[:8]
            with JOBS_LOCK:
                ACTIVE_JOBS[job_id] = {
                    "id": job_id,
                    "filename": filename,
                    "rel_path": rel_path,
                    "status": "queued",
                    "percent": 0,
                    "bytes_uploaded": 0,
                    "total_bytes": file_size,
                    "speed_str": "",
                    "eta_str": "",
                    "created_at": time.time()
                }
            thread = threading.Thread(
                target=background_upload_worker,
                args=(job_id, target_path, config["api_key"]),
                daemon=True
            )
            thread.start()

        return jsonify({
            "success": True,
            "filename": filename,
            "rel_path": rel_path,
            "size": format_bytes(file_size),
            "job_id": job_id
        })

    except Exception as e:
        logger.error("Failed to save incoming file '%s': %s", filename, e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/upload-to-pixeldrain", methods=["POST"])
def api_upload_to_pixeldrain():
    """Trigger background upload of an existing file in the watched folder (supports subfolders)."""
    data = request.get_json() or {}
    rel_path = data.get("rel_path") or data.get("filename", "")
    if not rel_path:
        return jsonify({"success": False, "error": "File path required"}), 400

    target = resolve_subpath(rel_path)
    if not target or not target.is_file():
        return jsonify({
            "success": False,
            "error_code": ErrorCode.FILE_NOT_FOUND.code_str,
            "error": f"File '{rel_path}' does not exist in the designated folder."
        }), 404

    config = load_config()
    api_key = config.get("api_key", "").strip()
    if not api_key:
        return jsonify({
            "success": False,
            "error_code": ErrorCode.AUTH_REQUIRED.code_str,
            "error": "Pixeldrain API key is not configured.",
            "hint": "Set your API key in the web settings panel."
        }), 400

    # Normalize the relative path key
    safe_rel = get_relative_path(target)

    # Check if this file is already uploading
    with JOBS_LOCK:
        for job in ACTIVE_JOBS.values():
            job_rel = job.get("rel_path", job.get("filename", ""))
            if job_rel == safe_rel and job.get("status") in ["queued", "uploading"]:
                return jsonify({"success": False, "error": "This file is already being uploaded.", "job_id": job["id"]}), 409

    job_id = str(uuid.uuid4())[:8]
    total_size = target.stat().st_size

    with JOBS_LOCK:
        ACTIVE_JOBS[job_id] = {
            "id": job_id,
            "filename": target.name,
            "rel_path": safe_rel,
            "status": "queued",
            "percent": 0,
            "bytes_uploaded": 0,
            "total_bytes": total_size,
            "speed_str": "Connecting...",
            "eta_str": "",
            "created_at": time.time()
        }

    thread = threading.Thread(
        target=background_upload_worker,
        args=(job_id, target, api_key),
        daemon=True
    )
    thread.start()

    return jsonify({"success": True, "job_id": job_id, "status": "queued"})


@app.route("/api/jobs", methods=["GET"])
def api_get_jobs():
    """Return all active and recent upload jobs."""
    with JOBS_LOCK:
        jobs_list = []
        for job_id, job in ACTIVE_JOBS.items():
            job_copy = {k: v for k, v in job.items() if k != "cancel_event"}
            jobs_list.append(job_copy)

    jobs_list.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return jsonify({"success": True, "jobs": jobs_list[:25]})


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_cancel_job(job_id: str):
    with JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404

        cancel_ev = job.get("cancel_event")
        if cancel_ev:
            cancel_ev.set()
            job["status"] = "cancelling"
            logger.info("Cancel requested for job %s (%s)", job_id, job.get("filename"))
            return jsonify({"success": True, "message": "Cancellation requested"})
        else:
            job["status"] = "cancelled"
            return jsonify({"success": True, "message": "Job cancelled"})


@app.route("/api/files/delete", methods=["POST"])
def api_delete_file():
    """Delete a file from the watched folder (supports subfolders)."""
    data = request.get_json() or {}
    rel_path = data.get("rel_path", "").strip()

    if not rel_path:
        return jsonify({"success": False, "error": "File path required."}), 400

    target = resolve_subpath(rel_path)
    if not target or not target.is_file():
        return jsonify({"success": False, "error": f"File '{rel_path}' not found."}), 404

    try:
        target.unlink()
        history = load_history()
        # Remove by rel_path or bare filename
        for key in [rel_path, target.name]:
            if key in history:
                del history[key]
        save_history(history)

        logger.info("Deleted file '%s' from %s", rel_path, WATCHED_FOLDER)
        return jsonify({"success": True, "message": f"Deleted '{rel_path}'"})
    except Exception as e:
        logger.error("Failed to delete '%s': %s", rel_path, e)
        return jsonify({"success": False, "error": str(e)}), 500


# Keep legacy DELETE route for backwards compatibility
@app.route("/api/files/<path:filename>", methods=["DELETE"])
def api_delete_file_legacy(filename: str):
    """Legacy DELETE route: delete a file by name from the root watched folder."""
    target_path = get_safe_file_path(filename)
    if not target_path or not target_path.is_file():
        return jsonify({"success": False, "error": f"File '{filename}' not found."}), 404

    try:
        target_path.unlink()
        history = load_history()
        for key in [filename, get_relative_path(target_path)]:
            if key in history:
                del history[key]
        save_history(history)

        logger.info("Deleted file '%s' from %s", filename, WATCHED_FOLDER)
        return jsonify({"success": True, "message": f"Deleted '{filename}'"})
    except Exception as e:
        logger.error("Failed to delete '%s': %s", filename, e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    lines_count = int(request.args.get("lines", 150))
    if not DEFAULT_LOG_FILE.exists():
        return jsonify({"success": True, "lines": ["Log file is empty or not yet created."]})

    try:
        with open(DEFAULT_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return jsonify({"success": True, "lines": all_lines[-lines_count:]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/logs/clear", methods=["POST"])
def api_clear_logs():
    try:
        with open(DEFAULT_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"Log reset at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        return jsonify({"success": True, "message": "Log cleared."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# MAIN LAUNCHER
# ============================================================================

def main():
    global WATCHED_FOLDER

    parser = argparse.ArgumentParser(
        description="Raspberry Pi Web Interface for Pixeldrain Uploader",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-d", "--folder",
        default=os.environ.get("PIXELDRAIN_FOLDER", str(DEFAULT_STORAGE_FOLDER)),
        help="Path to the single designated folder to expose (default: ./storage)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host address to bind to (default: 0.0.0.0 for LAN access)"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=int(os.environ.get("PORT", 5000)),
        help="Port number (default: 5000)"
    )
    parser.add_argument(
        "-k", "--key",
        default=None,
        help="Pixeldrain API key"
    )
    parser.add_argument(
        "-l", "--log",
        action="store_true",
        help="Enable file logging to pixeldrain.log"
    )

    args = parser.parse_args()

    WATCHED_FOLDER = Path(args.folder).resolve()
    WATCHED_FOLDER.mkdir(parents=True, exist_ok=True)

    config = load_config()
    if args.key:
        config["api_key"] = args.key
    config["folder"] = str(WATCHED_FOLDER)
    if args.log:
        config["logging_enabled"] = True
    save_config(config)

    setup_logging(enabled=config.get("logging_enabled", False))

    print("=" * 65)
    print("  🚀 Pixeldrain Raspberry Pi Web Interface")
    print("=" * 65)
    print(f"  • Designated Folder : {WATCHED_FOLDER}")
    print(f"  • Local Web Access  : http://localhost:{args.port}")
    print(f"  • LAN Web Access    : http://<raspberry-pi-ip>:{args.port}")
    print(f"  • API Key Configured: {'Yes' if config.get('api_key') else 'No (set via Web UI)'}")
    print(f"  • File Logging      : {'Enabled' if config.get('logging_enabled') else 'Disabled'}")
    print("=" * 65)
    print("  Press CTRL+C to stop the server.\n")

    logger.info("Web app started on %s:%d (Scoped Folder: %s)", args.host, args.port, WATCHED_FOLDER)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
