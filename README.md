# Pixeldrain Uploader

A lightweight file upload toolkit for [Pixeldrain](https://pixeldrain.com) featuring:
- **Raspberry Pi Web Interface** with scoped single-folder access, dropzone, and background upload manager.
- **Desktop GUI** with modern dark/light mode and clipboard integration.
- **CLI Tool** with streaming chunk uploads, real-time progress bars, structured error codes (`E1001` - `E9999`), and file logging.

---

## 1. Quick Start

### Installation

```bash
pip install -r requirements.txt
```

*(Installs `flask`, `requests`, `tqdm`, and `customtkinter`)*

### Getting an API Key

Pixeldrain requires an API key for API file uploads:
1. Log in to [Pixeldrain](https://pixeldrain.com).
2. Go to [https://pixeldrain.com/user/api_keys](https://pixeldrain.com/user/api_keys).
3. Generate a key and copy it.

---

## 2. Raspberry Pi Web Interface (`web_app.py`)

Designed to run on headless Raspberry Pis or home servers. It restricts access strictly to a **single designated folder** on your drive, allowing you to access it from any browser on your home Wi-Fi/LAN (phone, laptop, tablet).

### Launching the Web Server

```bash
# Default (folder ./storage on port 5000)
python web_app.py

# Specify custom folder and port:
python web_app.py --folder "/home/pi/my_shared_folder" --port 5000
```

Open in any browser:
* Local: `http://localhost:5000`
* LAN: `http://<raspberry-pi-ip>:5000`

### Web Features:
* **Single-Folder Scoped Access:** The web interface is strictly sandboxed to the specified folder with path traversal protection.
* **Direct Browser Uploads:** Drag-and-drop or select files from your browser to save them directly into the Raspberry Pi folder.
* **One-Click Pixeldrain Upload:** Upload any file in the folder to Pixeldrain with live speed, percentage bar, and ETA.
* **Persistent Link History:** Shareable Pixeldrain links (`https://pixeldrain.com/u/<id>`) are remembered even across reboots.
* **In-Browser Logs & Diagnostics:** View backend logs and detailed error code explanations (`E1001` - `E9999`) directly in the browser.

### Running Automatically on RPi Boot (systemd)

A service template [pixeldrain.service](file:///c:/Users/hp/Desktop/mediafire/pixeldrain.service) is included:

1. Copy the project folder to your Raspberry Pi (e.g. `/home/pi/pixeldrain`).
2. Copy the service unit:
   ```bash
   sudo cp pixeldrain.service /etc/systemd/system/
   ```
3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable pixeldrain
   sudo systemctl start pixeldrain
   ```
4. Check status:
   ```bash
   sudo systemctl status pixeldrain
   ```

---

## 3. Desktop GUI (`gui.py`)

For desktop operating systems (Windows, macOS, Linux with desktop):

```bash
python gui.py
```

* Drag-and-drop queue, real-time progress bars, and persistent API key in `config.json`.
* Header toggle for file logging and "📄 View Log" button.
* "Details ℹ" popups on failed files explaining the error code and resolution.

---

## 4. Command-Line CLI (`pixeldrain_uploader.py`)

```bash
# Basic upload
python pixeldrain_uploader.py "path/to/my_file.zip"

# With logging enabled
python pixeldrain_uploader.py "file.mp4" --log --debug

# Multiple files & auto-copy link
python pixeldrain_uploader.py "doc1.pdf" "video.mp4" --copy

# Structured JSON output (for automation scripts)
python pixeldrain_uploader.py "backup.tar.gz" --json
```

---

## 5. Error Codes Reference

| Error Code | Numeric ID | Category | Description | Remediation |
|---|---|---|---|---|
| `ERR_FILE_NOT_FOUND` | **`E1001`** | File | Local file does not exist | Verify file path |
| `ERR_FILE_READ_ERROR` | **`E1002`** | File | Could not read file | Check permissions or file locks |
| `ERR_FILE_EMPTY` | **`E1003`** | File | 0-byte file | Pixeldrain rejects empty uploads |
| `ERR_AUTH_REQUIRED` | **`E2001`** | Auth | API key missing | Set key in Settings or CLI |
| `ERR_AUTH_INVALID` | **`E2002`** | Auth | Invalid or expired API key | Verify key at pixeldrain.com/user/api_keys |
| `ERR_RATE_LIMIT` | **`E3001`** | Quota | Rate limit reached | Wait a few minutes |
| `ERR_FILE_TOO_LARGE` | **`E3002`** | Quota | File exceeds limit | Standard limit is 10 GB per file |
| `ERR_NETWORK_TIMEOUT`| **`E4001`** | Network | Request timed out | Check network connectivity |
| `ERR_NETWORK_ERROR` | **`E4002`** | Network | Connection failed | Check network, DNS, or firewall |
| `ERR_SERVER_ERROR` | **`E5001`** | Server | HTTP 5xx error | Pixeldrain service temporary issue |
| `ERR_UPLOAD_CANCELLED`| **`E6001`** | Action | Cancelled by user | Operation aborted |
| `ERR_UNKNOWN` | **`E9999`** | Fallback | Unclassified error | Check application logs |
