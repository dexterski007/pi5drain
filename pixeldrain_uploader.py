#!/usr/bin/env python3
"""
pixeldrain_uploader.py
----------------------
A robust Python script and CLI tool for uploading files to Pixeldrain.

Features:
- Streaming chunk uploads (low memory usage even for multi-GB files)
- Real-time upload progress bar (with transfer speed and ETA)
- Structured Error Codes taxonomy (E1001 - E9999)
- Configurable Logging Switch (File and Console logging)
- Interactive CLI with support for multiple files
- Environment variable / .env / CLI argument API key support
- Importable as a Python module for use in other projects
- JSON output mode for scripting and pipelines
"""

import argparse
from enum import Enum
import json
import logging
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import requests
except ImportError:
    print("Error: The 'requests' library is required. Install it using:\n  pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm import tqdm
    from tqdm.utils import CallbackIOWrapper
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False


PIXELDRAIN_UPLOAD_URL = "https://pixeldrain.com/api/file"
PIXELDRAIN_FILE_URL_PREFIX = "https://pixeldrain.com/u"
PIXELDRAIN_API_URL_PREFIX = "https://pixeldrain.com/api/file"
DEFAULT_LOG_FILE = Path("pixeldrain.log")

# Module logger
logger = logging.getLogger("pixeldrain")


# ============================================================================
# ERROR CODE SYSTEM
# ============================================================================

class ErrorCode(Enum):
    """
    Standardized Error Codes Taxonomy for Pixeldrain Operations.
    """
    # File errors (1001 - 1099)
    FILE_NOT_FOUND = (
        "ERR_FILE_NOT_FOUND",
        1001,
        "Local file not found.",
        "Verify that the specified path exists and is accessible."
    )
    FILE_READ_ERROR = (
        "ERR_FILE_READ_ERROR",
        1002,
        "Could not read the local file.",
        "Check file read permissions or whether another program is locking it."
    )
    FILE_EMPTY = (
        "ERR_FILE_EMPTY",
        1003,
        "File is empty (0 bytes).",
        "Pixeldrain rejects uploads of 0-byte files."
    )

    # Auth errors (2001 - 2099)
    AUTH_REQUIRED = (
        "ERR_AUTH_REQUIRED",
        2001,
        "Pixeldrain API key is required.",
        "Generate a free API key at https://pixeldrain.com/user/api_keys"
    )
    AUTH_INVALID = (
        "ERR_AUTH_INVALID",
        2002,
        "The provided Pixeldrain API key is invalid, revoked, or expired.",
        "Verify your key at https://pixeldrain.com/user/api_keys"
    )

    # Limits and Quotas (3001 - 3099)
    RATE_LIMIT_EXCEEDED = (
        "ERR_RATE_LIMIT",
        3001,
        "Pixeldrain API rate limit exceeded.",
        "Wait a few minutes before attempting further uploads."
    )
    FILE_TOO_LARGE = (
        "ERR_FILE_TOO_LARGE",
        3002,
        "File size exceeds Pixeldrain account limit.",
        "The file is larger than allowed (standard account limit is 10 GB per file)."
    )

    # Network / Connectivity (4001 - 4099)
    NETWORK_TIMEOUT = (
        "ERR_NETWORK_TIMEOUT",
        4001,
        "The network request timed out.",
        "Check your internet stability and try again."
    )
    NETWORK_ERROR = (
        "ERR_NETWORK_ERROR",
        4002,
        "Failed to establish a network connection to Pixeldrain.",
        "Verify network connectivity, firewall, or proxy settings."
    )

    # Server Errors (5001 - 5099)
    SERVER_ERROR = (
        "ERR_SERVER_ERROR",
        5001,
        "Pixeldrain server returned an internal error.",
        "Pixeldrain servers may be experiencing temporary degradation. Try again shortly."
    )

    # User Actions (6001 - 6099)
    UPLOAD_CANCELLED = (
        "ERR_UPLOAD_CANCELLED",
        6001,
        "Upload was cancelled by user.",
        "The operation was aborted before completion."
    )

    # Unknown / Unclassified (9999)
    UNKNOWN_ERROR = (
        "ERR_UNKNOWN",
        9999,
        "An unexpected error occurred.",
        "Inspect the application log file for traceback details."
    )

    def __init__(self, code_str: str, numeric_code: int, description: str, hint: str):
        self.code_str = code_str
        self.numeric_code = numeric_code
        self.description = description
        self.hint = hint


class PixeldrainUploadError(Exception):
    """
    Standardized Exception with Error Code, status, and troubleshooting hint.
    """
    def __init__(
        self,
        code: ErrorCode,
        message: Optional[str] = None,
        http_status: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        self.code = code
        self.message = message or code.description
        self.http_status = http_status
        self.details = details or {}

        hint_text = f" (Hint: {code.hint})" if code.hint else ""
        super().__init__(f"[{code.code_str} / E{code.numeric_code}] {self.message}{hint_text}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert error to a structured dictionary for JSON reporting."""
        return {
            "success": False,
            "error_code": self.code.code_str,
            "error_number": self.code.numeric_code,
            "message": self.message,
            "hint": self.code.hint,
            "http_status": self.http_status,
            "details": self.details
        }


# ============================================================================
# LOGGING SYSTEM
# ============================================================================

def setup_logging(
    enabled: bool = False,
    log_file: Optional[Path | str] = None,
    verbose: bool = False,
    log_to_console: bool = False
) -> logging.Logger:
    """
    Configure the 'pixeldrain' logger.

    :param enabled: Whether file logging is activated.
    :param log_file: File path for the log file (defaults to 'pixeldrain.log').
    :param verbose: If True, log level is DEBUG; otherwise INFO.
    :param log_to_console: If True, write log records to stderr.
    :return: Configured logging.Logger.
    """
    pkg_logger = logging.getLogger("pixeldrain")
    pkg_logger.handlers.clear()

    level = logging.DEBUG if verbose else logging.INFO
    pkg_logger.setLevel(level)

    if not enabled and not log_to_console:
        pkg_logger.addHandler(logging.NullHandler())
        return pkg_logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-5s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    if enabled:
        target_path = Path(log_file or DEFAULT_LOG_FILE).resolve()
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(target_path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            pkg_logger.addHandler(file_handler)
            pkg_logger.info("Logging started. Level=%s, Target='%s'", logging.getLevelName(level), target_path)
        except Exception as e:
            print(f"Warning: Failed to create log file at '{target_path}': {e}", file=sys.stderr)

    if log_to_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        pkg_logger.addHandler(console_handler)

    return pkg_logger


# ============================================================================
# PROGRESS STREAM WRAPPER
# ============================================================================

class ChunkProgressWrapper:
    """Wraps a file object to report chunk progress, log milestones, and support cancellation."""
    def __init__(self, file_obj, total_size: int, filename: str = "", callback=None, cancel_event=None):
        self._file = file_obj
        self.total_size = total_size
        self.filename = filename
        self.bytes_read = 0
        self.callback = callback
        self.cancel_event = cancel_event
        self._last_logged_pct = -1

    def read(self, size=-1):
        if self.cancel_event and self.cancel_event.is_set():
            logger.warning("Upload of '%s' cancelled during streaming read.", self.filename)
            raise PixeldrainUploadError(ErrorCode.UPLOAD_CANCELLED)

        chunk = self._file.read(size)
        if chunk:
            chunk_len = len(chunk)
            self.bytes_read += chunk_len
            if self.callback:
                self.callback(chunk_len, self.bytes_read, self.total_size)

            # Log milestone every 25% if logger is at DEBUG level
            if self.total_size > 0:
                pct = int((self.bytes_read / self.total_size) * 100)
                if pct % 25 == 0 and pct != self._last_logged_pct:
                    self._last_logged_pct = pct
                    logger.debug("Streaming '%s': %d%% (%d / %d bytes)", self.filename, pct, self.bytes_read, self.total_size)

        return chunk


# ============================================================================
# UPLOADER ENGINE
# ============================================================================

class PixeldrainUploader:
    """
    Client for interacting with the Pixeldrain API.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Pixeldrain client.
        
        :param api_key: Optional Pixeldrain API key. If omitted, checks the
                        PIXELDRAIN_API_KEY environment variable.
        """
        self.api_key = api_key or os.environ.get("PIXELDRAIN_API_KEY")
        if self.api_key:
            masked = f"{self.api_key[:4]}...{self.api_key[-4:]}" if len(self.api_key) > 8 else "***"
            logger.debug("PixeldrainUploader initialized with API Key: %s", masked)
        else:
            logger.debug("PixeldrainUploader initialized without API Key")

    @property
    def auth(self) -> Optional[tuple]:
        """Returns HTTP Basic Auth tuple for Pixeldrain: ('', api_key)."""
        if self.api_key:
            return ("", self.api_key)
        return None

    def upload(
        self,
        file_path: str | Path,
        custom_name: Optional[str] = None,
        show_progress: bool = True,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
        cancel_event: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Upload a file to Pixeldrain.

        :param file_path: Path to the local file to upload.
        :param custom_name: Optional remote filename to assign on Pixeldrain.
        :param show_progress: Whether to display a CLI progress bar.
        :param progress_callback: Optional callable(bytes_delta, total_sent, total_size).
        :param cancel_event: Optional threading.Event to abort upload.
        :return: Dict containing upload details (id, view_url, api_url, filename, size_bytes).
        """
        path = Path(file_path).resolve()

        # 1. Validation checks
        if not path.exists():
            err = PixeldrainUploadError(
                ErrorCode.FILE_NOT_FOUND,
                f"File not found: '{file_path}'"
            )
            logger.error("Validation error: %s", err)
            raise err

        if path.is_dir():
            err = PixeldrainUploadError(
                ErrorCode.FILE_READ_ERROR,
                f"Path '{file_path}' is a directory, not a file."
            )
            logger.error("Validation error: %s", err)
            raise err

        try:
            file_size = path.stat().st_size
        except OSError as e:
            err = PixeldrainUploadError(
                ErrorCode.FILE_READ_ERROR,
                f"Could not read file metadata for '{file_path}': {e}"
            )
            logger.error("Metadata read error: %s", err)
            raise err

        if file_size == 0:
            err = PixeldrainUploadError(
                ErrorCode.FILE_EMPTY,
                f"File '{path.name}' is 0 bytes. Pixeldrain does not allow empty file uploads."
            )
            logger.error("Validation error: %s", err)
            raise err

        filename = custom_name or path.name
        encoded_filename = urllib.parse.quote(filename)
        url = f"{PIXELDRAIN_UPLOAD_URL}/{encoded_filename}"

        headers = {
            "Content-Length": str(file_size)
        }

        logger.info("Uploading '%s' (%s, %d bytes) -> %s", filename, format_bytes(file_size), file_size, url)

        # 2. Open and stream upload
        try:
            file_obj = open(path, "rb")
        except (PermissionError, OSError) as e:
            err = PixeldrainUploadError(
                ErrorCode.FILE_READ_ERROR,
                f"Permission denied or file locked: '{path.name}': {e}"
            )
            logger.error("File open error: %s", err)
            raise err

        with file_obj:
            try:
                if progress_callback or cancel_event:
                    wrapped_file = ChunkProgressWrapper(
                        file_obj,
                        total_size=file_size,
                        filename=filename,
                        callback=progress_callback,
                        cancel_event=cancel_event
                    )
                    response = requests.put(
                        url,
                        data=wrapped_file,
                        auth=self.auth,
                        headers=headers,
                        timeout=(15, 600)  # (connect timeout, read timeout)
                    )
                elif show_progress and TQDM_AVAILABLE:
                    with tqdm(
                        total=file_size,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=f"Uploading {filename}",
                        leave=True
                    ) as pbar:
                        wrapped_file = CallbackIOWrapper(pbar.update, file_obj, "read")
                        response = requests.put(
                            url,
                            data=wrapped_file,
                            auth=self.auth,
                            headers=headers,
                            timeout=(15, 600)
                        )
                else:
                    response = requests.put(
                        url,
                        data=file_obj,
                        auth=self.auth,
                        headers=headers,
                        timeout=(15, 600)
                    )
            except requests.exceptions.Timeout as e:
                err = PixeldrainUploadError(
                    ErrorCode.NETWORK_TIMEOUT,
                    f"Upload connection timed out: {e}"
                )
                logger.error("Network timeout for '%s': %s", filename, err)
                raise err
            except requests.exceptions.ConnectionError as e:
                err = PixeldrainUploadError(
                    ErrorCode.NETWORK_ERROR,
                    f"Connection to Pixeldrain failed: {e}"
                )
                logger.error("Connection error for '%s': %s", filename, err)
                raise err
            except requests.exceptions.RequestException as e:
                err = PixeldrainUploadError(
                    ErrorCode.NETWORK_ERROR,
                    f"Network request error: {e}"
                )
                logger.error("Request error for '%s': %s", filename, err)
                raise err

        # 3. Parse response
        try:
            res_json = response.json()
        except ValueError:
            res_json = {"raw": response.text}

        logger.debug("Pixeldrain HTTP %d response: %s", response.status_code, res_json)

        # 4. Handle success (HTTP 200 or 201 with an ID or success flag)
        file_id = res_json.get("id")
        if response.status_code in (200, 201) and (file_id or res_json.get("success")):
            remote_name = res_json.get("name", filename)
            remote_size = res_json.get("size", file_size)
            view_url = f"{PIXELDRAIN_FILE_URL_PREFIX}/{file_id}"
            download_url = f"{PIXELDRAIN_API_URL_PREFIX}/{file_id}?download"
            logger.info("Upload succeeded for '%s'! ID: %s | URL: %s", remote_name, file_id, view_url)
            return {
                "id": file_id,
                "filename": remote_name,
                "size_bytes": remote_size,
                "view_url": view_url,
                "download_url": download_url,
                "api_url": f"{PIXELDRAIN_API_URL_PREFIX}/{file_id}",
                "info_url": f"{PIXELDRAIN_API_URL_PREFIX}/{file_id}/info",
                "success": True,
                "response": res_json
            }

        # 5. Handle HTTP status failures with standardized ErrorCodes
        error_msg = res_json.get("message", response.text)
        error_val = res_json.get("value", "")

        # Authentication issues (401)
        if response.status_code == 401 or "authentication" in error_val:
            if not self.api_key or error_val == "authentication_required":
                err = PixeldrainUploadError(
                    ErrorCode.AUTH_REQUIRED,
                    message=error_msg,
                    http_status=response.status_code,
                    details=res_json
                )
            else:
                err = PixeldrainUploadError(
                    ErrorCode.AUTH_INVALID,
                    message=error_msg,
                    http_status=response.status_code,
                    details=res_json
                )
            logger.error("Auth error for '%s': %s", filename, err)
            raise err

        # Rate limits (429)
        if response.status_code == 429:
            err = PixeldrainUploadError(
                ErrorCode.RATE_LIMIT_EXCEEDED,
                message=error_msg,
                http_status=response.status_code,
                details=res_json
            )
            logger.error("Rate limit reached: %s", err)
            raise err

        # File too large (413)
        if response.status_code == 413:
            err = PixeldrainUploadError(
                ErrorCode.FILE_TOO_LARGE,
                message=error_msg,
                http_status=response.status_code,
                details=res_json
            )
            logger.error("File too large: %s", err)
            raise err

        # Server errors (500+)
        if response.status_code >= 500:
            err = PixeldrainUploadError(
                ErrorCode.SERVER_ERROR,
                message=f"Pixeldrain server returned status {response.status_code}: {error_msg}",
                http_status=response.status_code,
                details=res_json
            )
            logger.error("Server error: %s", err)
            raise err

        # Fallback unknown error
        err = PixeldrainUploadError(
            ErrorCode.UNKNOWN_ERROR,
            message=f"Upload rejected with HTTP {response.status_code}: {error_msg}",
            http_status=response.status_code,
            details=res_json
        )
        logger.error("Unclassified upload failure: %s", err)
        raise err


def upload_file(
    file_path: str | Path,
    api_key: Optional[str] = None,
    custom_name: Optional[str] = None,
    show_progress: bool = True
) -> Dict[str, Any]:
    """
    Convenience function to upload a single file to Pixeldrain.
    """
    uploader = PixeldrainUploader(api_key=api_key)
    return uploader.upload(file_path=file_path, custom_name=custom_name, show_progress=show_progress)


# ============================================================================
# UTILITIES
# ============================================================================

def copy_to_clipboard(text: str) -> bool:
    """Copies text to the system clipboard (Windows, macOS, Linux)."""
    try:
        if sys.platform == "win32":
            subprocess.run("clip", input=text.encode("utf-16"), check=True)
            return True
        elif sys.platform == "darwin":
            subprocess.run("pbcopy", input=text.encode("utf-8"), check=True)
            return True
        else:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True)
            return True
    except Exception as e:
        logger.debug("Failed to copy to clipboard: %e", e)
        return False


def format_bytes(size: int) -> str:
    """Format byte count into human-readable representation."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def load_env_file():
    """Load key-value pairs from a local .env file if present."""
    env_path = Path(".env")
    if env_path.is_file():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            logger.debug("Could not read .env file: %s", e)


# ============================================================================
# CLI ENTRY POINT
# ============================================================================

def main():
    load_env_file()

    parser = argparse.ArgumentParser(
        description="Upload files to Pixeldrain with real-time progress, error codes, and logging.",
        epilog="Examples:\n"
               "  python pixeldrain_uploader.py document.pdf\n"
               "  python pixeldrain_uploader.py archive.zip --key YOUR_API_KEY\n"
               "  python pixeldrain_uploader.py video.mp4 --log --debug\n"
               "  python pixeldrain_uploader.py data.csv --json\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "files",
        nargs="+",
        help="One or more file paths to upload"
    )
    parser.add_argument(
        "-k", "--key",
        dest="api_key",
        default=os.environ.get("PIXELDRAIN_API_KEY"),
        help="Pixeldrain API key (default: read from PIXELDRAIN_API_KEY env var)"
    )
    parser.add_argument(
        "-n", "--name",
        dest="custom_name",
        default=None,
        help="Custom filename to set on Pixeldrain (only applicable when uploading a single file)"
    )
    parser.add_argument(
        "-c", "--copy",
        action="store_true",
        help="Copy the download URL of the uploaded file to clipboard"
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress progress bar and non-essential output"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output upload results as structured JSON (including error codes)"
    )
    # Logging switches
    parser.add_argument(
        "-l", "--log",
        action="store_true",
        help=f"Enable file logging (writes to '{DEFAULT_LOG_FILE}')"
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        help="Specify a custom destination path for the log file"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG level logging (verbose internal state and HTTP events)"
    )

    args = parser.parse_args()

    # Configure logging switch
    log_enabled = args.log or bool(args.log_file) or args.debug
    setup_logging(
        enabled=log_enabled,
        log_file=args.log_file,
        verbose=args.debug,
        log_to_console=False
    )

    if log_enabled:
        logger.info("CLI invocation started: %s", " ".join(sys.argv))

    # Prompt for API key if missing and running interactively
    api_key = args.api_key
    if not api_key:
        if sys.stdin.isatty() and not args.json:
            print("No Pixeldrain API key found.")
            print("You can get one for free at: https://pixeldrain.com/user/api_keys")
            try:
                entered = input("Enter your Pixeldrain API Key (or press Enter to cancel): ").strip()
                if entered:
                    api_key = entered
                else:
                    auth_err = PixeldrainUploadError(ErrorCode.AUTH_REQUIRED)
                    logger.error("Aborted by user without API key.")
                    print(f"Error: {auth_err}", file=sys.stderr)
                    sys.exit(1)
            except (KeyboardInterrupt, EOFError):
                sys.exit(1)
        else:
            auth_err = PixeldrainUploadError(ErrorCode.AUTH_REQUIRED)
            logger.error("Missing API key: %s", auth_err)
            if args.json:
                print(json.dumps(auth_err.to_dict(), indent=2))
            else:
                print(f"Error: {auth_err}", file=sys.stderr)
            sys.exit(1)

    uploader = PixeldrainUploader(api_key=api_key)
    results: List[Dict[str, Any]] = []
    has_error = False

    if args.custom_name and len(args.files) > 1:
        print("Warning: --name is ignored when uploading multiple files.", file=sys.stderr)

    for idx, file_str in enumerate(args.files, 1):
        file_path = Path(file_str)
        custom_name = args.custom_name if len(args.files) == 1 else None

        try:
            if not args.quiet and not args.json and len(args.files) > 1:
                print(f"\n[{idx}/{len(args.files)}] Uploading {file_path.name}...")

            res = uploader.upload(
                file_path=file_path,
                custom_name=custom_name,
                show_progress=(not args.quiet and not args.json)
            )
            results.append(res)

            if not args.json:
                if not args.quiet:
                    print("\nUpload Complete!")
                    print(f"  File:          {res['filename']} ({format_bytes(res['size_bytes'])})")
                    print(f"  View / Page:   {res['view_url']}")
                    print(f"  Direct Link:   {res['download_url']}")
                    print(f"  File ID:       {res['id']}")
                else:
                    print(res['view_url'])

            if args.copy and res.get("view_url"):
                if copy_to_clipboard(res["view_url"]):
                    if not args.quiet and not args.json:
                        print("  [Copied URL to clipboard]")

        except PixeldrainUploadError as err:
            has_error = True
            err_dict = err.to_dict()
            err_dict["file"] = str(file_path)
            results.append(err_dict)

            if not args.json:
                print(f"Error uploading '{file_path.name}': {err}", file=sys.stderr)

        except Exception as ex:
            has_error = True
            unknown_err = PixeldrainUploadError(ErrorCode.UNKNOWN_ERROR, message=str(ex))
            err_dict = unknown_err.to_dict()
            err_dict["file"] = str(file_path)
            results.append(err_dict)

            logger.exception("Unhandled exception during upload: %s", ex)
            if not args.json:
                print(f"Error uploading '{file_path.name}': {unknown_err}", file=sys.stderr)

    if args.json:
        if len(results) == 1:
            print(json.dumps(results[0], indent=2))
        else:
            print(json.dumps(results, indent=2))

    if has_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
