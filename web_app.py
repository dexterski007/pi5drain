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
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
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

# Remote ("from the web") import limits
URL_IMPORT_MAX_REDIRECTS = 5
URL_IMPORT_CHUNK_SIZE = 1024 * 1024
URL_IMPORT_TIMEOUT = (15, 60)  # (connect seconds, read seconds)
URL_IMPORT_MIN_FREE_BYTES = 64 * 1024 * 1024
URL_IMPORT_MAX_URL_LENGTH = 4096


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
# REMOTE URL IMPORT ("ADD FROM WEB")
# ============================================================================

class UrlImportError(Exception):
    """Raised when a remote URL cannot be imported into the watched folder."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_remote_url(url: str) -> None:
    """
    Validate an http(s) URL before fetching it.
    Blocks loopback, link-local, multicast, reserved and unspecified targets so a
    link cannot be used to reach services on the Pi/host itself (e.g. cloud metadata).
    Private LAN addresses are allowed, since importing from a local NAS is a valid use case.
    """
    if not url:
        raise UrlImportError("Paste a web link first.")

    if len(url) > URL_IMPORT_MAX_URL_LENGTH:
        raise UrlImportError("That link is too long.")

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise UrlImportError(f"Invalid link: {exc}")

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlImportError("Only http:// and https:// links are supported.")

    host = parts.hostname
    if not host:
        raise UrlImportError("That link has no hostname.")

    if host.lower() in ("localhost", "localhost.localdomain"):
        raise UrlImportError("Links to localhost are blocked.")

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UrlImportError("That link has an invalid port.")

    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise UrlImportError(f"Could not resolve the host '{host}'.")

    for _family, _type, _proto, _canonname, sockaddr in addresses:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue

        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped

        if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_unspecified or ip.is_reserved):
            raise UrlImportError(
                "That link points to a local or reserved network address, which is blocked."
            )


def filename_from_content_disposition(header: Optional[str]) -> Optional[str]:
    """Extract a filename from a Content-Disposition header (including RFC 5987)."""
    if not header:
        return None

    match = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", header, flags=re.IGNORECASE)
    if match:
        charset = (match.group(1) or "utf-8").strip() or "utf-8"
        value = match.group(2).strip().strip('"')
        try:
            return urllib.parse.unquote(value, encoding=charset, errors="replace")
        except LookupError:
            return urllib.parse.unquote(value)

    match = re.search(r'filename\s*=\s*("([^"]*)"|[^;]+)', header, flags=re.IGNORECASE)
    if match:
        value = match.group(2) if match.group(2) is not None else match.group(1)
        return value.strip().strip('"')

    return None


def extract_remote_filename(response: requests.Response, url: str) -> str:
    """Pick the best filename for a downloaded URL: header first, then URL path."""
    candidate = filename_from_content_disposition(response.headers.get("Content-Disposition"))

    if not candidate:
        path = urllib.parse.urlsplit(response.url or url).path
        candidate = urllib.parse.unquote(path.rsplit("/", 1)[-1])

    if candidate:
        # Some servers send a full path; keep only the final component.
        candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1]

    return candidate or f"download-{datetime.now():%Y%m%d-%H%M%S}"


def unique_destination(parent: Path, filename: str) -> Path:
    """Return a non-existing path in parent, adding ' (1)', ' (2)', ... if needed."""
    candidate = parent / filename
    if not candidate.exists():
        return candidate

    stem, suffix = os.path.splitext(filename)
    for index in range(1, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate

    raise UrlImportError("Too many files with the same name in this folder.", 409)


def fetch_url_to_folder(url: str, parent: Path) -> Tuple[Path, int]:
    """
    Download a remote URL into `parent` and return (path, bytes_written).
    Redirects are followed manually so every hop is validated. Data is written to a
    '.part' file first and only renamed into place once the download succeeds.
    """
    session = requests.Session()
    response: Optional[requests.Response] = None
    current_url = url

    try:
        for _hop in range(URL_IMPORT_MAX_REDIRECTS + 1):
            validate_remote_url(current_url)

            try:
                response = session.get(
                    current_url,
                    stream=True,
                    allow_redirects=False,
                    timeout=URL_IMPORT_TIMEOUT,
                    headers={
                        "User-Agent": "Pi5drain/1.0 (web uploader)",
                        "Accept": "*/*",
                        "Accept-Encoding": "identity",
                    },
                )
            except requests.exceptions.Timeout:
                raise UrlImportError("The web server took too long to respond.", 504)
            except requests.exceptions.SSLError:
                raise UrlImportError("The link's HTTPS certificate could not be verified.", 502)
            except requests.exceptions.RequestException as exc:
                raise UrlImportError(f"Could not reach that link: {exc}", 502)

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location:
                    raise UrlImportError("The link redirected without a destination.", 502)
                current_url = urllib.parse.urljoin(current_url, location)
                continue

            break
        else:
            raise UrlImportError("The link redirected too many times.")

        if response.status_code != 200:
            status = response.status_code
            response.close()
            response = None
            raise UrlImportError(f"The web server returned HTTP {status}.", 502)

        raw_name = extract_remote_filename(response, current_url)
        filename = sanitize_name(raw_name) or f"download-{datetime.now():%Y%m%d-%H%M%S}"
        target = unique_destination(parent, filename)

        content_length: Optional[int] = None
        header_length = response.headers.get("Content-Length")
        if header_length:
            try:
                content_length = int(header_length)
            except ValueError:
                content_length = None

        if content_length == 0:
            raise UrlImportError("That link returned an empty file.")

        free_bytes = shutil.disk_usage(parent).free
        if content_length is not None and content_length > free_bytes - URL_IMPORT_MIN_FREE_BYTES:
            raise UrlImportError("Not enough free disk space for that file.", 507)

        temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
        bytes_written = 0
        logger.info("Importing URL '%s' -> '%s'", current_url, target.name)

        try:
            with open(temp_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=URL_IMPORT_CHUNK_SIZE):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if bytes_written > free_bytes - URL_IMPORT_MIN_FREE_BYTES:
                        raise UrlImportError("Not enough free disk space for that file.", 507)

            if bytes_written == 0:
                raise UrlImportError("That link returned an empty file.")

            os.replace(temp_path, target)

        except UrlImportError:
            temp_path.unlink(missing_ok=True)
            raise
        except requests.exceptions.RequestException as exc:
            temp_path.unlink(missing_ok=True)
            raise UrlImportError(f"The download was interrupted: {exc}", 502)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise UrlImportError(f"Could not save the file: {exc}", 500)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        logger.info("Imported '%s' from the web (%s)", target.name, format_bytes(bytes_written))
        return target, bytes_written

    finally:
        if response is not None:
            response.close()
        session.close()


def queue_pixeldrain_upload(file_path: Path, rel_path: str, api_key: str) -> str:
    """Register a background Pixeldrain upload job and start its worker thread."""
    job_id = str(uuid.uuid4())[:8]
    total_size = file_path.stat().st_size

    with JOBS_LOCK:
        ACTIVE_JOBS[job_id] = {
            "id": job_id,
            "filename": file_path.name,
            "rel_path": rel_path,
            "status": "queued",
            "percent": 0,
            "bytes_uploaded": 0,
            "total_bytes": total_size,
            "speed_str": "Connecting...",
            "eta_str": "",
            "created_at": time.time(),
        }

    thread = threading.Thread(
        target=background_upload_worker,
        args=(job_id, file_path, api_key),
        daemon=True,
    )
    thread.start()
    return job_id


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
            job_id = queue_pixeldrain_upload(target_path, rel_path, config["api_key"])

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


@app.route("/api/upload-from-url", methods=["POST"])
def api_upload_from_url():
    """Download a file from a public web link into the folder (optionally upload it)."""
    data = request.get_json(silent=True) or {}
    raw_url = str(data.get("url", "")).strip()
    subpath = str(data.get("path", "")).strip()

    if not raw_url:
        return jsonify({"success": False, "error": "Paste a web link first."}), 400

    parent = resolve_subpath(subpath)
    if parent is None:
        return jsonify({"success": False, "error": "Invalid path (traversal blocked)."}), 403
    if not parent.exists() or not parent.is_dir():
        return jsonify({"success": False, "error": "Destination folder does not exist."}), 404

    try:
        target_path, file_size = fetch_url_to_folder(raw_url, parent)
    except UrlImportError as err:
        logger.warning("URL import failed for '%s': %s", raw_url, err.message)
        return jsonify({"success": False, "error": err.message}), err.status_code
    except Exception as exc:
        logger.exception("Unexpected URL import error: %s", exc)
        return jsonify({"success": False, "error": "Unexpected error while importing that link."}), 500

    rel_path = get_relative_path(target_path)
    config = load_config()
    job_id = None
    auto_upload = data.get("auto_upload") is True or config.get("auto_upload_on_add", False)
    if auto_upload and config.get("api_key"):
        job_id = queue_pixeldrain_upload(target_path, rel_path, config["api_key"])

    return jsonify({
        "success": True,
        "filename": target_path.name,
        "rel_path": rel_path,
        "size": format_bytes(file_size),
        "size_bytes": file_size,
        "job_id": job_id
    })


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

    job_id = queue_pixeldrain_upload(target, safe_rel, api_key)

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

def configure_console_encoding() -> None:
    """Avoid UnicodeEncodeError on legacy Windows code pages (cp1252/cp437)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main():
    global WATCHED_FOLDER

    configure_console_encoding()

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
