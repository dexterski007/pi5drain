#!/usr/bin/env python3
"""
gui.py
------
Modern graphical desktop interface for Pixeldrain Uploader using CustomTkinter.
Supports batch uploads, real-time progress & speed tracking, cancellation,
logging switch, structured error codes system, link copying, and local API key storage.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Dict, List, Optional

import customtkinter as ctk

# Import uploader engine and error system from pixeldrain_uploader.py
try:
    from pixeldrain_uploader import (
        DEFAULT_LOG_FILE,
        ErrorCode,
        PixeldrainUploadError,
        PixeldrainUploader,
        copy_to_clipboard,
        format_bytes,
        load_env_file,
        setup_logging,
    )
except ImportError:
    messagebox.showerror(
        "Import Error",
        "Could not import pixeldrain_uploader.py. Ensure both files are in the same folder."
    )
    sys.exit(1)

CONFIG_FILE = Path(__file__).parent / "settings.json" if (Path(__file__).parent / "settings.json").exists() else Path(__file__).parent / "config.json"
logger = logging.getLogger("pixeldrain")


def load_config() -> Dict[str, Any]:
    """Load configuration from config.json or fallback to .env."""
    config = {
        "api_key": "",
        "auto_copy": True,
        "logging_enabled": False,
        "theme": "Dark"
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
    """Save configuration to config.json."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Warning: Failed to save config: {e}", file=sys.stderr)


class FileItem:
    """Represents a file in the upload queue with error tracking."""
    def __init__(self, file_path: Path):
        self.path = file_path
        self.name = file_path.name
        self.size = file_path.stat().st_size if file_path.exists() else 0
        self.status = "Queued"  # Queued, Uploading, Completed, Failed, Cancelled
        self.result: Optional[Dict[str, Any]] = None

        # Structured error codes
        self.error_code: Optional[str] = None
        self.error_number: Optional[int] = None
        self.error_hint: Optional[str] = None
        self.error_msg: Optional[str] = None

        # UI card references
        self.frame: Optional[ctk.CTkFrame] = None
        self.status_label: Optional[ctk.CTkLabel] = None
        self.action_btn: Optional[ctk.CTkButton] = None
        self.remove_btn: Optional[ctk.CTkButton] = None


class PixeldrainApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.config_data = load_config()

        # Window Setup
        self.title("Pixeldrain Uploader")
        self.geometry("860x700")
        self.minsize(740, 580)

        # Apply Theme
        saved_theme = self.config_data.get("theme", "Dark")
        ctk.set_appearance_mode(saved_theme)
        ctk.set_default_color_theme("blue")

        # Initialize Logging based on saved config
        self.logging_enabled = self.config_data.get("logging_enabled", False)
        setup_logging(enabled=self.logging_enabled)
        if self.logging_enabled:
            logger.info("Pixeldrain GUI initialized with logging enabled.")

        # State
        self.files_queue: List[FileItem] = []
        self.is_uploading = False
        self.cancel_event = threading.Event()
        self.upload_thread: Optional[threading.Thread] = None

        # Speed tracking variables
        self.last_time = 0.0
        self.last_bytes = 0
        self.current_file_bytes = 0

        self._build_ui()
        self._update_queue_ui()

    def _build_ui(self):
        # 1. Top Header Frame
        header_frame = ctk.CTkFrame(self, corner_radius=10)
        header_frame.pack(fill="x", padx=16, pady=(14, 8))

        # Title & Subtitle
        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.pack(side="left", padx=14, pady=10)

        title_lbl = ctk.CTkLabel(
            title_box,
            text="Pixeldrain Uploader",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        title_lbl.pack(anchor="w")

        sub_lbl = ctk.CTkLabel(
            title_box,
            text="Fast cloud uploads with speed tracking, logging & error diagnostics",
            font=ctk.CTkFont(size=12),
            text_color="gray"
        )
        sub_lbl.pack(anchor="w")

        # Right header controls (Theme & Logging Switch)
        top_right_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        top_right_box.pack(side="right", padx=14, pady=10)

        # Logging Switch
        self.log_switch_var = ctk.BooleanVar(value=self.logging_enabled)
        self.log_switch = ctk.CTkSwitch(
            top_right_box,
            text="Logging",
            variable=self.log_switch_var,
            command=self._toggle_logging_switch,
            width=80,
            font=ctk.CTkFont(size=12)
        )
        self.log_switch.pack(side="left", padx=(0, 10))

        # View Log File button
        self.view_log_btn = ctk.CTkButton(
            top_right_box,
            text="📄 View Log",
            width=80,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color="gray30",
            hover_color="gray40",
            command=self._open_log_file
        )
        self.view_log_btn.pack(side="left", padx=(0, 12))

        # Theme selector
        theme_lbl = ctk.CTkLabel(top_right_box, text="Theme:", font=ctk.CTkFont(size=12))
        theme_lbl.pack(side="left", padx=(0, 6))

        self.theme_menu = ctk.CTkOptionMenu(
            top_right_box,
            values=["Dark", "Light", "System"],
            command=self._on_theme_change,
            width=85,
            height=28
        )
        self.theme_menu.set(self.config_data.get("theme", "Dark"))
        self.theme_menu.pack(side="left")

        # 2. API Key Section
        api_frame = ctk.CTkFrame(self, corner_radius=10)
        api_frame.pack(fill="x", padx=16, pady=6)

        key_lbl = ctk.CTkLabel(api_frame, text="API Key:", font=ctk.CTkFont(weight="bold"))
        key_lbl.pack(side="left", padx=(14, 8), pady=10)

        self.api_key_entry = ctk.CTkEntry(
            api_frame,
            placeholder_text="Enter Pixeldrain API Key (UUID format)",
            show="•",
            width=360
        )
        if self.config_data.get("api_key"):
            self.api_key_entry.insert(0, self.config_data["api_key"])
        self.api_key_entry.pack(side="left", fill="x", expand=True, padx=4, pady=10)

        self.show_key_btn = ctk.CTkButton(
            api_frame,
            text="Show",
            width=50,
            command=self._toggle_show_key,
            fg_color="gray30",
            hover_color="gray40"
        )
        self.show_key_btn.pack(side="left", padx=4, pady=10)

        self.save_key_btn = ctk.CTkButton(
            api_frame,
            text="Save Key",
            width=70,
            command=self._save_api_key
        )
        self.save_key_btn.pack(side="left", padx=4, pady=10)

        get_key_btn = ctk.CTkButton(
            api_frame,
            text="Get Key ↗",
            width=80,
            fg_color="transparent",
            border_width=1,
            border_color=("gray60", "gray40"),
            command=lambda: webbrowser.open("https://pixeldrain.com/user/api_keys")
        )
        get_key_btn.pack(side="left", padx=(4, 14), pady=10)

        # 3. Actions / File Queue Toolbar
        toolbar_frame = ctk.CTkFrame(self, fg_color="transparent")
        toolbar_frame.pack(fill="x", padx=16, pady=(8, 4))

        self.add_files_btn = ctk.CTkButton(
            toolbar_frame,
            text="📁 Add Files",
            width=110,
            command=self._add_files
        )
        self.add_files_btn.pack(side="left", padx=(0, 8))

        self.add_folder_btn = ctk.CTkButton(
            toolbar_frame,
            text="📂 Add Folder",
            width=110,
            fg_color=("gray75", "gray25"),
            text_color=("black", "white"),
            hover_color=("gray65", "gray35"),
            command=self._add_folder
        )
        self.add_folder_btn.pack(side="left", padx=(0, 8))

        self.clear_queue_btn = ctk.CTkButton(
            toolbar_frame,
            text="🗑 Clear All",
            width=90,
            fg_color="transparent",
            text_color=("firebrick2", "salmon"),
            border_width=1,
            border_color=("firebrick2", "salmon"),
            hover_color=("gray90", "gray25"),
            command=self._clear_queue
        )
        self.clear_queue_btn.pack(side="left")

        # Auto-copy checkbox
        self.auto_copy_var = ctk.BooleanVar(value=self.config_data.get("auto_copy", True))
        self.auto_copy_cb = ctk.CTkCheckBox(
            toolbar_frame,
            text="Auto-copy link",
            variable=self.auto_copy_var,
            command=self._on_options_changed
        )
        self.auto_copy_cb.pack(side="right", padx=(8, 0))

        # Copy all links button
        self.copy_all_btn = ctk.CTkButton(
            toolbar_frame,
            text="📋 Copy All Links",
            width=120,
            command=self._copy_all_links,
            state="disabled",
            fg_color="gray30"
        )
        self.copy_all_btn.pack(side="right", padx=6)

        # 4. Scrollable Queue Frame
        self.queue_frame = ctk.CTkScrollableFrame(self, label_text="Upload Queue (0 files)")
        self.queue_frame.pack(fill="both", expand=True, padx=16, pady=6)

        self.empty_label = ctk.CTkLabel(
            self.queue_frame,
            text="No files added yet.\nClick 'Add Files' or 'Add Folder' to queue files for upload.",
            font=ctk.CTkFont(size=14),
            text_color="gray"
        )
        self.empty_label.pack(pady=60)

        # 5. Progress Section
        progress_card = ctk.CTkFrame(self, corner_radius=10)
        progress_card.pack(fill="x", padx=16, pady=6)

        # Stats Labels
        stats_top = ctk.CTkFrame(progress_card, fg_color="transparent")
        stats_top.pack(fill="x", padx=14, pady=(8, 2))

        self.status_text_lbl = ctk.CTkLabel(
            stats_top,
            text="Ready",
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.status_text_lbl.pack(side="left")

        self.speed_lbl = ctk.CTkLabel(
            stats_top,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="gray"
        )
        self.speed_lbl.pack(side="right")

        # Main Progress Bar
        self.progress_bar = ctk.CTkProgressBar(progress_card, height=12)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", padx=14, pady=4)

        # Progress Detail
        stats_bottom = ctk.CTkFrame(progress_card, fg_color="transparent")
        stats_bottom.pack(fill="x", padx=14, pady=(2, 8))

        self.detail_lbl = ctk.CTkLabel(
            stats_bottom,
            text="0 / 0 files",
            font=ctk.CTkFont(size=12),
            text_color="gray"
        )
        self.detail_lbl.pack(side="left")

        self.eta_lbl = ctk.CTkLabel(
            stats_bottom,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="gray"
        )
        self.eta_lbl.pack(side="right")

        # 6. Bottom Execution Buttons
        bottom_bar = ctk.CTkFrame(self, fg_color="transparent")
        bottom_bar.pack(fill="x", padx=16, pady=(4, 14))

        self.start_btn = ctk.CTkButton(
            bottom_bar,
            text="🚀 Start Upload",
            height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._start_upload
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.cancel_btn = ctk.CTkButton(
            bottom_bar,
            text="⏹ Cancel",
            width=100,
            height=40,
            font=ctk.CTkFont(size=14),
            fg_color="firebrick3",
            hover_color="firebrick4",
            command=self._cancel_upload,
            state="disabled"
        )
        self.cancel_btn.pack(side="right")

    # ---------------- Logging Controls ---------------- #

    def _toggle_logging_switch(self):
        enabled = self.log_switch_var.get()
        self.logging_enabled = enabled
        self.config_data["logging_enabled"] = enabled
        save_config(self.config_data)

        setup_logging(enabled=enabled)
        if enabled:
            logger.info("Logging switch turned ON by user in GUI.")
        else:
            print("Logging switch turned OFF in GUI.")

    def _open_log_file(self):
        log_path = DEFAULT_LOG_FILE.resolve()
        if not log_path.exists():
            # Create an initial log file if missing
            try:
                log_path.write_text("Pixeldrain Log Initialized.\n", encoding="utf-8")
            except Exception as e:
                messagebox.showerror("Error", f"Could not create log file: {e}")
                return

        try:
            if sys.platform == "win32":
                os.startfile(str(log_path))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(log_path)], check=True)
            else:
                subprocess.run(["xdg-open", str(log_path)], check=True)
        except Exception as e:
            messagebox.showerror("Error", f"Could not open log file '{log_path}': {e}")

    # ---------------- UI Event Handlers ---------------- #

    def _toggle_show_key(self):
        if self.api_key_entry.cget("show") == "•":
            self.api_key_entry.configure(show="")
            self.show_key_btn.configure(text="Hide")
        else:
            self.api_key_entry.configure(show="•")
            self.show_key_btn.configure(text="Show")

    def _save_api_key(self):
        key = self.api_key_entry.get().strip()
        self.config_data["api_key"] = key
        save_config(self.config_data)
        if self.logging_enabled:
            logger.info("Pixeldrain API Key updated and saved to config.")
        messagebox.showinfo("Saved", "Pixeldrain API Key saved successfully.")

    def _on_theme_change(self, new_theme: str):
        ctk.set_appearance_mode(new_theme)
        self.config_data["theme"] = new_theme
        save_config(self.config_data)

    def _on_options_changed(self):
        self.config_data["auto_copy"] = self.auto_copy_var.get()
        save_config(self.config_data)

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select Files to Upload",
            filetypes=[("All Files", "*.*")]
        )
        if not paths:
            return

        added_count = 0
        existing_paths = {f.path for f in self.files_queue}
        for p in paths:
            fpath = Path(p)
            if fpath.is_file() and fpath not in existing_paths:
                self.files_queue.append(FileItem(fpath))
                added_count += 1
                if self.logging_enabled:
                    logger.debug("Queued file: %s (%d bytes)", fpath.name, fpath.stat().st_size)

        if added_count > 0:
            self._update_queue_ui()

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Select Folder to Upload")
        if not folder:
            return

        folder_path = Path(folder)
        existing_paths = {f.path for f in self.files_queue}
        added_count = 0

        for root, _, files in os.walk(folder_path):
            for file in files:
                fpath = Path(root) / file
                if fpath.is_file() and fpath not in existing_paths:
                    self.files_queue.append(FileItem(fpath))
                    added_count += 1

        if added_count > 0:
            if self.logging_enabled:
                logger.info("Queued %d files from folder: %s", added_count, folder_path)
            self._update_queue_ui()

    def _clear_queue(self):
        if self.is_uploading:
            messagebox.showwarning("Busy", "Cannot clear queue while uploads are in progress.")
            return

        self.files_queue.clear()
        self._update_queue_ui()
        self.progress_bar.set(0)
        self.status_text_lbl.configure(text="Ready")
        self.detail_lbl.configure(text="0 / 0 files")
        self.speed_lbl.configure(text="")
        self.eta_lbl.configure(text="")
        self.copy_all_btn.configure(state="disabled", fg_color="gray30")

    def _remove_file_item(self, item: FileItem):
        if self.is_uploading:
            return
        if item in self.files_queue:
            self.files_queue.remove(item)
            self._update_queue_ui()

    def _show_error_dialog(self, item: FileItem):
        """Display a structured error code diagnostic window for a failed item."""
        code_str = item.error_code or "ERR_UNKNOWN"
        num_str = f"E{item.error_number}" if item.error_number else "E9999"
        msg = item.error_msg or "Unknown error occurred."
        hint = item.error_hint or "Check pixeldrain.log for more details."

        diag = ctk.CTkToplevel(self)
        diag.title(f"Error Diagnostic: {item.name}")
        diag.geometry("520x320")
        diag.minsize(440, 260)
        diag.grab_set()

        content = ctk.CTkFrame(diag, corner_radius=10)
        content.pack(fill="both", expand=True, padx=16, pady=16)

        # Header badge
        badge = ctk.CTkLabel(
            content,
            text=f"[{code_str} / {num_str}]",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="firebrick2"
        )
        badge.pack(anchor="w", padx=14, pady=(12, 4))

        file_lbl = ctk.CTkLabel(
            content,
            text=f"File: {item.name}",
            font=ctk.CTkFont(size=12, weight="bold")
        )
        file_lbl.pack(anchor="w", padx=14, pady=2)

        # Message box
        msg_title = ctk.CTkLabel(content, text="Error Message:", font=ctk.CTkFont(size=11, weight="bold"), text_color="gray")
        msg_title.pack(anchor="w", padx=14, pady=(8, 2))

        msg_box = ctk.CTkTextbox(content, height=65, wrap="word")
        msg_box.insert("1.0", msg)
        msg_box.configure(state="disabled")
        msg_box.pack(fill="x", padx=14, pady=2)

        # Troubleshooting Hint
        hint_title = ctk.CTkLabel(content, text="Troubleshooting Suggestion:", font=ctk.CTkFont(size=11, weight="bold"), text_color="gray")
        hint_title.pack(anchor="w", padx=14, pady=(8, 2))

        hint_lbl = ctk.CTkLabel(content, text=hint, wraplength=460, justify="left", font=ctk.CTkFont(size=12))
        hint_lbl.pack(anchor="w", padx=14, pady=2)

        # Close button
        btn_close = ctk.CTkButton(content, text="Close", width=90, command=diag.destroy)
        btn_close.pack(anchor="e", padx=14, pady=(10, 8))

    def _update_queue_ui(self):
        # Clear existing cards in scrollable frame
        for widget in self.queue_frame.winfo_children():
            widget.destroy()

        count = len(self.files_queue)
        self.queue_frame.configure(label_text=f"Upload Queue ({count} file{'s' if count != 1 else ''})")

        if count == 0:
            self.empty_label = ctk.CTkLabel(
                self.queue_frame,
                text="No files added yet.\nClick 'Add Files' or 'Add Folder' to queue files for upload.",
                font=ctk.CTkFont(size=14),
                text_color="gray"
            )
            self.empty_label.pack(pady=60)
            self.detail_lbl.configure(text="0 / 0 files")
            return

        completed_count = sum(1 for f in self.files_queue if f.status == "Completed")
        if completed_count > 0:
            self.copy_all_btn.configure(state="normal", fg_color=["#3B8ED0", "#1F6AA5"])
        else:
            self.copy_all_btn.configure(state="disabled", fg_color="gray30")

        self.detail_lbl.configure(text=f"{completed_count} / {count} uploaded")

        # Render each file card
        for item in self.files_queue:
            card = ctk.CTkFrame(self.queue_frame, corner_radius=8)
            card.pack(fill="x", padx=4, pady=4)
            item.frame = card

            # Left side: File Name + Size
            left_box = ctk.CTkFrame(card, fg_color="transparent")
            left_box.pack(side="left", fill="x", expand=True, padx=10, pady=8)

            name_lbl = ctk.CTkLabel(
                left_box,
                text=item.name,
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w"
            )
            name_lbl.pack(fill="x")

            size_lbl = ctk.CTkLabel(
                left_box,
                text=format_bytes(item.size),
                font=ctk.CTkFont(size=11),
                text_color="gray",
                anchor="w"
            )
            size_lbl.pack(fill="x")

            # Right side: Status badge & Action buttons
            right_box = ctk.CTkFrame(card, fg_color="transparent")
            right_box.pack(side="right", padx=10, pady=8)

            # Determine badge text and color
            status_color = "gray"
            status_text = item.status

            if item.status == "Completed":
                status_color = "SeaGreen3"
            elif item.status == "Uploading":
                status_color = "DodgerBlue"
            elif item.status == "Failed":
                status_color = "firebrick2"
                num = f"E{item.error_number}" if item.error_number else "ERR"
                status_text = f"Failed [{num}]"
            elif item.status == "Cancelled":
                status_color = "orange"
                status_text = "Cancelled [E6001]"

            status_lbl = ctk.CTkLabel(
                right_box,
                text=status_text,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=status_color
            )
            status_lbl.pack(side="left", padx=8)
            item.status_label = status_lbl

            # Failed item: show diagnostic button
            if item.status in ["Failed", "Cancelled"]:
                info_btn = ctk.CTkButton(
                    right_box,
                    text="Details ℹ",
                    width=68,
                    height=26,
                    font=ctk.CTkFont(size=11),
                    fg_color="firebrick3",
                    hover_color="firebrick4",
                    command=lambda it=item: self._show_error_dialog(it)
                )
                info_btn.pack(side="left", padx=4)

            # Completed item: show Copy Link & Open Link
            if item.status == "Completed" and item.result:
                copy_btn = ctk.CTkButton(
                    right_box,
                    text="Copy Link",
                    width=75,
                    height=26,
                    font=ctk.CTkFont(size=11),
                    command=lambda res=item.result: self._copy_single_link(res["view_url"])
                )
                copy_btn.pack(side="left", padx=4)

                open_btn = ctk.CTkButton(
                    right_box,
                    text="Open ↗",
                    width=60,
                    height=26,
                    font=ctk.CTkFont(size=11),
                    fg_color="transparent",
                    border_width=1,
                    command=lambda res=item.result: webbrowser.open(res["view_url"])
                )
                open_btn.pack(side="left", padx=4)

            # Remove button
            if not self.is_uploading:
                del_btn = ctk.CTkButton(
                    right_box,
                    text="✕",
                    width=28,
                    height=26,
                    fg_color="transparent",
                    hover_color=("gray85", "gray30"),
                    text_color=("gray40", "gray70"),
                    command=lambda it=item: self._remove_file_item(it)
                )
                del_btn.pack(side="left", padx=(4, 0))

    # ---------------- Upload Logic ---------------- #

    def _start_upload(self):
        if self.is_uploading:
            return

        api_key = self.api_key_entry.get().strip()
        if not api_key:
            err = PixeldrainUploadError(ErrorCode.AUTH_REQUIRED)
            if self.logging_enabled:
                logger.error("Upload attempt blocked: %s", err)
            messagebox.showerror(
                f"Error: {err.code.code_str} (E{err.code.numeric_code})",
                f"{err.message}\n\nHint: {err.code.hint}"
            )
            return

        pending_items = [f for f in self.files_queue if f.status in ["Queued", "Failed", "Cancelled"]]
        if not pending_items:
            messagebox.showinfo("Queue Empty", "No pending files in the queue to upload.")
            return

        # Lock UI
        self.is_uploading = True
        self.cancel_event.clear()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.add_files_btn.configure(state="disabled")
        self.add_folder_btn.configure(state="disabled")
        self.clear_queue_btn.configure(state="disabled")

        if self.logging_enabled:
            logger.info("Batch upload initiated: %d files pending.", len(pending_items))

        # Start background worker thread
        self.upload_thread = threading.Thread(
            target=self._upload_worker,
            args=(api_key, pending_items),
            daemon=True
        )
        self.upload_thread.start()

    def _cancel_upload(self):
        if self.is_uploading:
            self.cancel_event.set()
            self.status_text_lbl.configure(text="Cancelling upload [E6001]...")
            self.cancel_btn.configure(state="disabled")
            if self.logging_enabled:
                logger.warning("Upload cancellation triggered by user in GUI.")

    def _upload_worker(self, api_key: str, pending_items: List[FileItem]):
        uploader = PixeldrainUploader(api_key=api_key)
        total_items = len(self.files_queue)

        for current_idx, item in enumerate(self.files_queue):
            if item not in pending_items:
                continue

            if self.cancel_event.is_set():
                item.status = "Cancelled"
                item.error_code = ErrorCode.UPLOAD_CANCELLED.code_str
                item.error_number = ErrorCode.UPLOAD_CANCELLED.numeric_code
                item.error_msg = "Upload was cancelled by user."
                item.error_hint = ErrorCode.UPLOAD_CANCELLED.hint
                self.after(0, self._update_queue_ui)
                break

            # Update item status
            item.status = "Uploading"
            self.current_file_bytes = 0
            self.last_bytes = 0
            self.last_time = time.time()

            self.after(0, lambda it=item, idx=current_idx: self._on_file_upload_start(it, idx + 1, total_items))

            def progress_callback(chunk_delta: int, total_sent: int, total_size: int):
                self.after(0, lambda: self._on_chunk_progress(chunk_delta, total_sent, total_size))

            try:
                result = uploader.upload(
                    file_path=item.path,
                    show_progress=False,
                    progress_callback=progress_callback,
                    cancel_event=self.cancel_event
                )
                item.status = "Completed"
                item.result = result

                # Optional auto-copy
                if self.auto_copy_var.get() and result.get("view_url"):
                    copy_to_clipboard(result["view_url"])

            except PixeldrainUploadError as err:
                item.status = "Cancelled" if err.code == ErrorCode.UPLOAD_CANCELLED else "Failed"
                item.error_code = err.code.code_str
                item.error_number = err.code.numeric_code
                item.error_msg = err.message
                item.error_hint = err.code.hint
                if self.logging_enabled:
                    logger.error("Item '%s' failed with error [%s / E%d]: %s", item.name, err.code.code_str, err.code.numeric_code, err.message)

            except Exception as ex:
                item.status = "Failed"
                item.error_code = ErrorCode.UNKNOWN_ERROR.code_str
                item.error_number = ErrorCode.UNKNOWN_ERROR.numeric_code
                item.error_msg = str(ex)
                item.error_hint = ErrorCode.UNKNOWN_ERROR.hint
                if self.logging_enabled:
                    logger.exception("Unexpected error uploading '%s': %s", item.name, ex)

            self.after(0, self._update_queue_ui)

        self.after(0, self._on_upload_finished)

    def _on_file_upload_start(self, item: FileItem, current_num: int, total_num: int):
        self.status_text_lbl.configure(text=f"Uploading ({current_num}/{total_num}): {item.name}")
        self.progress_bar.set(0)
        self.speed_lbl.configure(text="Calculating speed...")
        self.eta_lbl.configure(text="")
        self._update_queue_ui()

    def _on_chunk_progress(self, chunk_delta: int, total_sent: int, total_size: int):
        if total_size <= 0:
            return

        fraction = min(total_sent / total_size, 1.0)
        self.progress_bar.set(fraction)

        # Calculate speed and ETA
        now = time.time()
        elapsed = now - self.last_time

        if elapsed >= 0.35:
            bytes_since_last = total_sent - self.last_bytes
            speed = bytes_since_last / elapsed if elapsed > 0 else 0
            self.last_bytes = total_sent
            self.last_time = now

            speed_str = f"{format_bytes(speed)}/s"
            self.speed_lbl.configure(text=speed_str)

            # ETA calculation
            remaining_bytes = max(0, total_size - total_sent)
            if speed > 0:
                eta_secs = int(remaining_bytes / speed)
                mins, secs = divmod(eta_secs, 60)
                hrs, mins = divmod(mins, 60)
                if hrs > 0:
                    eta_str = f"ETA: {hrs:02d}:{mins:02d}:{secs:02d}"
                else:
                    eta_str = f"ETA: {mins:02d}:{secs:02d}"
                self.eta_lbl.configure(text=eta_str)

        detail_text = f"{format_bytes(total_sent)} / {format_bytes(total_size)} ({int(fraction * 100)}%)"
        self.detail_lbl.configure(text=detail_text)

    def _on_upload_finished(self):
        self.is_uploading = False
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.add_files_btn.configure(state="normal")
        self.add_folder_btn.configure(state="normal")
        self.clear_queue_btn.configure(state="normal")

        if self.cancel_event.is_set():
            self.status_text_lbl.configure(text="Upload Cancelled [E6001]")
            self.speed_lbl.configure(text="")
            self.eta_lbl.configure(text="")
        else:
            completed = sum(1 for f in self.files_queue if f.status == "Completed")
            total = len(self.files_queue)
            failed = sum(1 for f in self.files_queue if f.status == "Failed")

            self.progress_bar.set(1.0 if completed == total else 0.0)
            if failed > 0:
                self.status_text_lbl.configure(text=f"Finished with {failed} error{'s' if failed > 1 else ''}")
            else:
                self.status_text_lbl.configure(text="All Uploads Completed Successfully!")

            self.speed_lbl.configure(text="")
            self.eta_lbl.configure(text="")

            if self.auto_copy_var.get() and completed == 1:
                first_comp = next((f for f in self.files_queue if f.status == "Completed"), None)
                if first_comp and first_comp.result:
                    self.detail_lbl.configure(text=f"Copied {first_comp.result['view_url']} to clipboard")

        self._update_queue_ui()

    def _copy_single_link(self, url: str):
        if copy_to_clipboard(url):
            messagebox.showinfo("Copied", f"Link copied to clipboard:\n{url}")
        else:
            self.clipboard_clear()
            self.clipboard_append(url)
            messagebox.showinfo("Copied", f"Link copied to clipboard:\n{url}")

    def _copy_all_links(self):
        completed_links = [
            f.result["view_url"]
            for f in self.files_queue
            if f.status == "Completed" and f.result and "view_url" in f.result
        ]
        if not completed_links:
            messagebox.showinfo("No Links", "No completed uploads to copy links from.")
            return

        all_text = "\n".join(completed_links)
        if copy_to_clipboard(all_text):
            messagebox.showinfo(
                "Copied",
                f"Successfully copied {len(completed_links)} link{'s' if len(completed_links) > 1 else ''} to clipboard!"
            )
        else:
            self.clipboard_clear()
            self.clipboard_append(all_text)
            messagebox.showinfo("Copied", f"Copied {len(completed_links)} links to clipboard!")


def main():
    app = PixeldrainApp()
    app.mainloop()


if __name__ == "__main__":
    main()
