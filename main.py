#!/usr/bin/env python3
"""
Android & Desktop Batch YouTube Video/Playlist Downloader (Kivy `main.py`)
==========================================================================
Fixes `ERROR: unable to download video data: HTTP Error 403: Forbidden` on Android:

Why the 403 Forbidden Error Happened & How This Fixes It:
1. Removed Broken `android_vr` and `ios` Clients:
   - YouTube blocks `android_vr` and unauthenticated `ios` stream downloads with
     `HTTP Error 403: Forbidden` (yt-dlp Issue #17456 / #15782).
   - This version uses `player_client=["android", "web_embedded"]` first (and
     falls back to `["web_embedded", "tv", "mweb"]` and `["default", "-android_vr", "-ios"]`),
     which serve pre-merged H.264 + AAC MP4 (`format 18`) without needing an
     external JavaScript runtime (`deno`) or FFmpeg!
2. Just-in-Time Per-Video Playlist Expansion (Fixes 30-Second URL Expiry 403s):
   - When you paste a Playlist URL, the app first expands the playlist using
     `extract_flat="in_playlist"` into individual video items in the queue.
   - Each video's stream URL is extracted milliseconds before its download begins
     (and auto-resumes with a fresh stream URL if a large video takes >30s),
     preventing stream URLs from expiring mid-playlist with `HTTP Error 403`.
3. In-App Over-The-Air (OTA) `yt-dlp` Updater (No APK Rebuild Needed):
   - Includes a "↻ Update Engine (OTA)" button (and auto-update on first 403)
     that downloads the latest pure-Python `yt-dlp` wheel directly from PyPI
     into the Android app's private storage and hot-reloads it on the phone.
4. Android 11–14 Scoped Storage Safe Saving:
   - Downloads into internal app staging storage first (with `updatetime=False`
     and 10 MB chunked HTTP requests) and exports finished `.mp4` files to
     `/storage/emulated/0/Download/YouTube_Downloads` + triggers Android's
     MediaScanner so videos appear immediately in Gallery / Downloads.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import json
import os
import queue
import re
import shutil
import ssl
import sys
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# ---------------------------------------------------------------------------
# SSL Certificate Setup for Android (python-for-android)
# ---------------------------------------------------------------------------
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    ssl._create_default_https_context = lambda: ssl.create_default_context(
        cafile=certifi.where()
    )
except ImportError:
    pass


def get_ota_engine_dir() -> Path:
    """Return writable directory for storing OTA-updated yt-dlp on Android or Desktop."""
    base = os.environ.get("ANDROID_PRIVATE") or os.path.expanduser("~")
    ota_dir = Path(base) / ".ytdlp_ota_pkg"
    ota_dir.mkdir(parents=True, exist_ok=True)
    return ota_dir


# Prepend OTA package directory if an updated yt-dlp was previously downloaded
_ota_dir = get_ota_engine_dir()
if (_ota_dir / "yt_dlp").is_dir() and str(_ota_dir) not in sys.path:
    sys.path.insert(0, str(_ota_dir))

try:
    import yt_dlp  # type: ignore

    HAS_YTDLP = True
except ImportError:
    yt_dlp = None  # type: ignore
    HAS_YTDLP = False


# ---------------------------------------------------------------------------
# Constants & Pure Helpers
# ---------------------------------------------------------------------------
QUALITY_PRESETS: List[str] = [
    "Best MP4 (H.264 + AAC)",
    "720p HD MP4 (H.264)",
    "480p / 360p MP4 (Fastest)",
    "Audio Only (M4A/AAC)",
]

# Client profiles ordered by reliability on Android (where no JS runtime or FFmpeg exists).
# NEVER include `android_vr` or `ios`, which trigger HTTP Error 403: Forbidden!
ANDROID_CLIENT_PROFILES: List[Tuple[str, List[str]]] = [
    ("android + web_embedded", ["android", "web_embedded"]),
    ("web_embedded + tv", ["web_embedded", "tv", "mweb"]),
    ("default (no android_vr)", ["default", "-android_vr", "-ios"]),
]

URL_REGEX = re.compile(r"https?://[^\s,;\"'<>]+", re.IGNORECASE)
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class DownloadCancelledError(Exception):
    """Raised inside a yt-dlp progress hook when the user cancels a download."""


def clean_ansi(text: str) -> str:
    """Strip ANSI escape sequences from yt-dlp messages."""
    return ANSI_ESCAPE_RE.sub("", text or "").strip()


def is_android() -> bool:
    """Return True when running inside an Android APK (python-for-android)."""
    return "ANDROID_ARGUMENT" in os.environ or "ANDROID_PRIVATE" in os.environ


def get_staging_download_dir() -> Path:
    """
    Return an internal app-private staging directory where yt-dlp can create
    `.part` files and rename them without hitting Android Scoped Storage errors.
    """
    base = os.environ.get("ANDROID_PRIVATE")
    if base:
        d = Path(base) / "yt_staging"
    else:
        d = Path(tempfile.gettempdir()) / "yt_batch_staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_default_download_dir() -> Path:
    """
    Resolve the public Android Downloads folder (`/storage/emulated/0/Download/YouTube_Downloads`)
    on Android devices, or `~/Downloads/YouTube_Downloads` on desktop systems.
    """
    if is_android():
        try:
            from android.storage import primary_external_storage_path  # type: ignore

            base = Path(primary_external_storage_path()) / "Download"
        except Exception:
            base = Path("/storage/emulated/0/Download")
        return base / "YouTube_Downloads"
    return Path.home() / "Downloads" / "YouTube_Downloads"


def request_android_permissions() -> None:
    """Request runtime storage permissions on Android if running on a phone."""
    if not is_android():
        return
    try:
        from android.permissions import Permission, request_permissions  # type: ignore

        perms = [
            Permission.INTERNET,
            Permission.WRITE_EXTERNAL_STORAGE,
            Permission.READ_EXTERNAL_STORAGE,
        ]
        for extra_perm in ("READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO"):
            if hasattr(Permission, extra_perm):
                perms.append(getattr(Permission, extra_perm))
        request_permissions(perms)
    except Exception:
        pass


def export_file_to_public_downloads(
    staged_file: Path,
    public_dir: Path,
    subfolder: str = "",
) -> Path:
    """
    Move a completed download from the app's private staging folder into the
    public Downloads directory (`/storage/emulated/0/Download/YouTube_Downloads`),
    with an automatic Android `MediaStore.Downloads` fallback for Android 11–14
    Scoped Storage, and trigger `MediaScannerConnection` so the video appears
    immediately in the phone's Gallery and Files apps.
    """
    target_dir = public_dir / subfolder if subfolder else public_dir
    target_path = target_dir / staged_file.name

    # 1. Try standard filesystem move/copy first
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(staged_file), str(target_path))
        staged_file.unlink(missing_ok=True)
        _notify_android_media_scanner(target_path)
        return target_path
    except Exception:
        pass

    # 2. Fallback for strict Android 10–14 Scoped Storage via MediaStore API
    if is_android():
        try:
            from jnius import autoclass  # type: ignore

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            MediaStoreDownloads = autoclass("android.provider.MediaStore$Downloads")
            MediaColumns = autoclass("android.provider.MediaStore$MediaColumns")
            ContentValues = autoclass("android.content.ContentValues")
            Environment = autoclass("android.os.Environment")

            context = PythonActivity.mActivity
            resolver = context.getContentResolver()
            values = ContentValues()
            values.put(MediaColumns.DISPLAY_NAME, staged_file.name)
            mime = (
                "audio/mp4"
                if staged_file.suffix.lower() in (".m4a", ".mp3")
                else "video/mp4"
            )
            values.put(MediaColumns.MIME_TYPE, mime)
            rel_path = Environment.DIRECTORY_DOWNLOADS + "/YouTube_Downloads"
            if subfolder:
                rel_path = f"{rel_path}/{subfolder}"
            values.put(MediaColumns.RELATIVE_PATH, rel_path)

            uri = resolver.insert(MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
            if uri is not None:
                out_stream = resolver.openOutputStream(uri)
                with open(staged_file, "rb") as in_fh:
                    while True:
                        chunk = in_fh.read(1024 * 1024)
                        if not chunk:
                            break
                        out_stream.write(chunk)
                out_stream.flush()
                out_stream.close()
                staged_file.unlink(missing_ok=True)
                return target_path
        except Exception:
            pass

    return staged_file


def _notify_android_media_scanner(file_path: Path) -> None:
    """Notify Android's MediaScanner so the new MP4 appears in Gallery/Files."""
    if not is_android():
        return
    try:
        from jnius import autoclass  # type: ignore

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        MediaScannerConnection = autoclass("android.media.MediaScannerConnection")
        context = PythonActivity.mActivity
        MediaScannerConnection.scanFile(
            context, [str(file_path)], None, None
        )
    except Exception:
        pass


def update_ytdlp_ota(log_callback: Optional[Any] = None) -> Tuple[bool, str]:
    """
    Download the latest `yt-dlp` pure-Python wheel (`.whl`) directly from PyPI
    into the app's writable storage directory and hot-reload `yt_dlp` in memory.
    Works inside an Android APK without needing `pip` or rebuilding the APK!
    """
    global yt_dlp, HAS_YTDLP
    try:
        if log_callback:
            log_callback("[OTA Updater] Checking PyPI for latest yt-dlp release...")

        req = urllib.request.Request(
            "https://pypi.org/pypi/yt-dlp/json",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        latest_ver = data["info"]["version"]
        wheel_url = None
        for file_info in data.get("urls", []):
            if file_info.get("packagetype") == "bdist_wheel" and file_info.get(
                "filename", ""
            ).endswith("-py3-none-any.whl"):
                wheel_url = file_info.get("url")
                break

        if not wheel_url:
            return False, "Could not find a pure-Python wheel on PyPI."

        if log_callback:
            log_callback(f"[OTA Updater] Downloading yt-dlp {latest_ver}...")

        ota_dir = get_ota_engine_dir()
        whl_path = ota_dir / "yt_dlp_latest.whl"
        urllib.request.urlretrieve(wheel_url, str(whl_path))

        # Extract wheel (which is a standard zip archive) into ota_dir
        old_pkg = ota_dir / "yt_dlp"
        if old_pkg.exists():
            shutil.rmtree(old_pkg, ignore_errors=True)

        with zipfile.ZipFile(whl_path, "r") as zf:
            zf.extractall(str(ota_dir))
        whl_path.unlink(missing_ok=True)

        if str(ota_dir) not in sys.path:
            sys.path.insert(0, str(ota_dir))

        # Purge cached yt_dlp submodules and reload fresh version
        for mod_name in list(sys.modules.keys()):
            if mod_name == "yt_dlp" or mod_name.startswith("yt_dlp."):
                sys.modules.pop(mod_name, None)

        import yt_dlp as _fresh_ytdlp  # type: ignore

        yt_dlp = _fresh_ytdlp
        HAS_YTDLP = True
        ver = getattr(yt_dlp.version, "__version__", latest_ver)
        return True, f"Updated yt-dlp to {ver}"
    except Exception as exc:
        return False, str(exc)


class YtDlpTaskLogger:
    """Captures yt-dlp errors and warnings so failed downloads never falsely show Completed."""

    def __init__(self, log_callback: Optional[Any] = None) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.log_callback = log_callback

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        cleaned = clean_ansi(msg)
        if cleaned:
            self.warnings.append(cleaned)

    def error(self, msg: str) -> None:
        cleaned = clean_ansi(msg)
        if cleaned:
            self.errors.append(cleaned)
            if self.log_callback:
                self.log_callback(cleaned)


@dataclass
class MobileDownloadTask:
    """State for a single queued video item."""

    task_id: str
    url: str
    is_playlist: bool
    quality_label: str
    title: str = ""
    playlist_folder: str = ""
    status: str = "Queued"
    progress_pct: float = 0.0
    speed_str: str = "—"
    eta_str: str = "—"
    playlist_badge: str = ""
    error_message: str = ""


def normalize_youtube_url(url: str) -> str:
    """
    Strip tracking parameters (`?si=...`, `&pp=...`) that can interfere with
    mobile extractor clients, and normalize `youtube.com` URLs.
    """
    try:
        parsed = urlparse(url.strip())
        query = parse_qs(parsed.query, keep_blank_values=False)
        for tracking_key in ("si", "pp", "feature", "ab_channel"):
            query.pop(tracking_key, None)
        clean_query = urlencode(query, doseq=True)
        netloc = parsed.netloc
        if netloc.lower() == "youtube.com":
            netloc = "www.youtube.com"
        return urlunparse(
            (parsed.scheme or "https", netloc, parsed.path, "", clean_query, "")
        )
    except Exception:
        return url.strip()


def extract_urls_from_text(raw_text: str) -> List[str]:
    """Extract, normalize, and deduplicate valid HTTP(S) URLs from pasted text."""
    found = URL_REGEX.findall(raw_text or "")
    seen = set()
    unique: List[str] = []
    for raw_url in found:
        cleaned = normalize_youtube_url(raw_url.rstrip(".,;)]}"))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def is_pure_playlist_url(url: str) -> bool:
    """Return True if the URL is a dedicated `/playlist?list=...` link."""
    try:
        parsed = urlparse(url.strip())
        query = parse_qs(parsed.query)
        has_list = bool(query.get("list"))
        has_video = bool(query.get("v")) or parsed.netloc.lower().endswith("youtu.be")
        if "/playlist" in parsed.path.lower() and has_list:
            return True
        if has_list and not has_video:
            return True
    except Exception:
        pass
    return False


def expand_playlist_flat(url: str) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Quickly fetch the list of `(video_url, title)` entries inside a YouTube
    playlist using `extract_flat='in_playlist'` without extracting video stream
    URLs yet (so stream URLs never expire before a video's turn in the queue!).
    """
    if not HAS_YTDLP:
        return "", []

    opts: Dict[str, Any] = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "nocheckcertificate": True,
    }
    entries_out: List[Tuple[str, str]] = []
    playlist_title = "Playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if isinstance(info, dict):
            playlist_title = str(info.get("title") or "YouTube_Playlist").strip()
            raw_entries = info.get("entries") or []
            for entry in raw_entries:
                if not isinstance(entry, dict):
                    continue
                vid_id = entry.get("id")
                vid_url = entry.get("url") or (
                    f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
                )
                if vid_url and not vid_url.startswith("http"):
                    vid_url = f"https://www.youtube.com/watch?v={vid_url}"
                vid_title = str(entry.get("title") or vid_url).strip()
                if vid_url and vid_title not in ("[Private video]", "[Deleted video]"):
                    entries_out.append((vid_url, vid_title))
    return playlist_title, entries_out


def build_mobile_ytdlp_options(
    output_dir: Path,
    quality_label: str,
    expand_playlists: bool = False,
    progress_hook: Optional[Any] = None,
    logger: Optional[Any] = None,
    player_clients: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Build `yt-dlp` options engineered for Android APK reliability:
    - Uses `player_clients=["android", "web_embedded"]` by default (never `android_vr`
      or `ios`, which return HTTP 403 Forbidden!).
    - Selects format `18` / pre-merged H.264 (`avc1`) + AAC (`mp4a`) MP4 streams
      when FFmpeg is absent so video and audio play natively on all Android phones.
    - Uses 10 MB chunked HTTP requests (`http_chunk_size=10485760`), `continuedl=True`,
      and `updatetime=False` (`--no-mtime`) to prevent YouTube CDN throttling and
      Android Scoped Storage `utime` permission errors.
    """
    output_dir = Path(output_dir).expanduser().resolve()
    ffmpeg_path = shutil.which("ffmpeg")
    has_sys_ffmpeg = ffmpeg_path is not None and not is_android()

    outtmpl = str(output_dir / "%(title).140B [%(id)s].%(ext)s")

    if player_clients is None:
        player_clients = ["android", "web_embedded"]

    opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": not expand_playlists,
        "ignoreerrors": False,
        "continuedl": True,
        "nopart": False,
        "updatetime": False,  # Critical on Android to avoid FUSE utime PermissionError
        "http_chunk_size": 10 * 1024 * 1024,  # 10 MB chunks prevent YouTube 403 throttling
        "retries": 6,
        "fragment_retries": 6,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "concurrent_fragment_downloads": 2,
        "windowsfilenames": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extractor_args": {"youtube": {"player_client": player_clients}},
    }

    if has_sys_ffmpeg and ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path

    if logger is not None:
        opts["logger"] = logger

    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]

    if "Audio Only" in quality_label:
        opts["format"] = "140/ba[ext=m4a]/ba[acodec^=mp4a]/ba/b"
        opts["format_sort"] = ["acodec:aac", "aext:m4a:mp3"]
        return opts

    # Video + Audio:
    if has_sys_ffmpeg:
        opts["format"] = (
            "bv*[vcodec^=avc]+ba[acodec^=mp4a]/"
            "bv*[vcodec^=avc]+ba/"
            "18/b[ext=mp4][vcodec!*=av01]/"
            "bv*[vcodec!*=av01]+ba/b"
        )
        opts["merge_output_format"] = "mp4"
    else:
        # On Android (no FFmpeg), format 18 and pre-merged MP4 streams contain
        # BOTH H.264 video and AAC audio in a single file without needing FFmpeg!
        opts["format"] = "18/b[ext=mp4][vcodec!*=av01]/b[ext=mp4]/b"

    sort_rules: List[str] = []
    if "720p" in quality_label:
        sort_rules.append("res:720")
    elif "480p" in quality_label:
        sort_rules.append("res:480")
    else:
        sort_rules.append("res:1080")

    sort_rules.extend(["vcodec:h264", "acodec:aac", "ext:mp4:m4a"])
    opts["format_sort"] = sort_rules
    return opts


# ---------------------------------------------------------------------------
# Kivy Mobile UI
# ---------------------------------------------------------------------------
def run_kivy_app() -> int:
    try:
        from kivy.app import App  # type: ignore
        from kivy.clock import Clock  # type: ignore
        from kivy.core.clipboard import Clipboard  # type: ignore
        from kivy.core.window import Window  # type: ignore
        from kivy.metrics import dp, sp  # type: ignore
        from kivy.uix.boxlayout import BoxLayout  # type: ignore
        from kivy.uix.button import Button  # type: ignore
        from kivy.uix.checkbox import CheckBox  # type: ignore
        from kivy.uix.label import Label  # type: ignore
        from kivy.uix.progressbar import ProgressBar  # type: ignore
        from kivy.uix.scrollview import ScrollView  # type: ignore
        from kivy.uix.spinner import Spinner  # type: ignore
        from kivy.uix.textinput import TextInput  # type: ignore
    except ImportError:
        sys.stderr.write(
            "Error: 'kivy' is not installed. To test on desktop, run:\n"
            "    pip install kivy yt-dlp certifi\n"
        )
        return 1

    Window.clearcolor = (0.96, 0.97, 0.99, 1)

    class YouTubeMobileApp(App):
        def build(self) -> Any:
            self.title = "YT Batch Downloader (H.264 MP4)"
            request_android_permissions()

            self.tasks: Dict[str, MobileDownloadTask] = {}
            self.task_order: List[str] = []
            self._counter = 0
            self.ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
            self.cancel_event = threading.Event()
            self.is_downloading = False
            self.did_auto_ota = False
            self.output_dir = get_default_download_dir()
            self.staging_dir = get_staging_download_dir()

            root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))

            # 1. Header + OTA Updater Button
            top_header = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            ver_str = getattr(yt_dlp.version, "__version__", "unknown") if HAS_YTDLP else "missing"
            self.header_lbl = Label(
                text=(
                    f"[b]YouTube Batch & Playlist Downloader[/b]\n"
                    f"[size=11sp]yt-dlp {ver_str}  •  H.264+AAC MP4 (Anti-403 Active)[/size]"
                ),
                markup=True,
                size_hint_x=0.68,
                color=(0.06, 0.09, 0.16, 1),
                halign="left",
                valign="middle",
            )
            self.header_lbl.bind(
                size=lambda inst, val: setattr(inst, "text_size", val)
            )

            self.ota_btn = Button(
                text="↻ Update Engine",
                size_hint_x=0.32,
                background_normal="",
                background_color=(0.15, 0.39, 0.92, 1),
                color=(1, 1, 1, 1),
                bold=True,
                font_size=sp(12),
            )
            self.ota_btn.bind(on_release=self._on_ota_update)

            top_header.add_widget(self.header_lbl)
            top_header.add_widget(self.ota_btn)
            root.add_widget(top_header)

            # 2. Multi-URL Input Box + Clipboard Actions
            btn_bar = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(6))
            paste_btn = Button(
                text="Paste Clipboard",
                background_normal="",
                background_color=(0.15, 0.39, 0.92, 1),
                color=(1, 1, 1, 1),
                font_size=sp(13),
            )
            paste_btn.bind(on_release=self._on_paste)

            clear_input_btn = Button(
                text="Clear Box",
                background_normal="",
                background_color=(0.40, 0.45, 0.55, 1),
                color=(1, 1, 1, 1),
                font_size=sp(13),
            )
            clear_input_btn.bind(
                on_release=lambda *_: setattr(self.url_input, "text", "")
            )

            add_btn = Button(
                text="+ Add to Queue",
                background_normal="",
                background_color=(0.08, 0.50, 0.24, 1),
                color=(1, 1, 1, 1),
                bold=True,
                font_size=sp(13),
            )
            add_btn.bind(on_release=self._on_add_urls)

            btn_bar.add_widget(paste_btn)
            btn_bar.add_widget(clear_input_btn)
            btn_bar.add_widget(add_btn)
            root.add_widget(btn_bar)

            self.url_input = TextInput(
                hint_text="Paste 10+ YouTube video links or a Playlist URL here...",
                multiline=True,
                size_hint_y=None,
                height=dp(82),
                font_size=sp(13),
                background_color=(1, 1, 1, 1),
                foreground_color=(0.06, 0.09, 0.16, 1),
            )
            root.add_widget(self.url_input)

            # 3. Quality & Playlist Controls
            opts_bar = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(8))
            self.quality_spinner = Spinner(
                text=QUALITY_PRESETS[0],
                values=QUALITY_PRESETS,
                size_hint_x=0.58,
                font_size=sp(12),
                background_normal="",
                background_color=(0.20, 0.25, 0.33, 1),
                color=(1, 1, 1, 1),
            )
            opts_bar.add_widget(self.quality_spinner)

            pl_box = BoxLayout(size_hint_x=0.42, spacing=dp(4))
            self.expand_pl_check = CheckBox(
                active=True, size_hint_x=None, width=dp(30)
            )
            pl_lbl = Label(
                text="Full Playlists",
                color=(0.06, 0.09, 0.16, 1),
                font_size=sp(12),
                halign="left",
                valign="middle",
            )
            pl_lbl.bind(size=lambda inst, val: setattr(inst, "text_size", val))
            pl_box.add_widget(self.expand_pl_check)
            pl_box.add_widget(pl_lbl)
            opts_bar.add_widget(pl_box)
            root.add_widget(opts_bar)

            # 4. Scrollable Queue List
            self.queue_label = Label(
                text=f"Save Path: {self.output_dir}",
                size_hint_y=None,
                height=dp(20),
                font_size=sp(11),
                color=(0.28, 0.33, 0.41, 1),
                halign="left",
                valign="middle",
            )
            self.queue_label.bind(
                size=lambda inst, val: setattr(inst, "text_size", val)
            )
            root.add_widget(self.queue_label)

            scroll = ScrollView(size_hint=(1, 1), bar_width=dp(6))
            self.queue_container = BoxLayout(
                orientation="vertical",
                size_hint_y=None,
                spacing=dp(6),
                padding=(0, dp(4)),
            )
            self.queue_container.bind(
                minimum_height=self.queue_container.setter("height")
            )
            scroll.add_widget(self.queue_container)
            root.add_widget(scroll)

            # 5. Live Log Box
            self.log_box = TextInput(
                text="[Ready] Paste links above and tap 'Start Batch Download'.\n",
                readonly=True,
                multiline=True,
                size_hint_y=None,
                height=dp(95),
                font_size=sp(11),
                background_color=(0.06, 0.09, 0.16, 1),
                foreground_color=(0.88, 0.91, 0.94, 1),
            )
            root.add_widget(self.log_box)

            # 6. Progress Bar & Start/Stop Footer
            self.progress_bar = ProgressBar(
                max=100, value=0, size_hint_y=None, height=dp(12)
            )
            root.add_widget(self.progress_bar)

            action_bar = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
            self.start_btn = Button(
                text="Start Batch Download",
                background_normal="",
                background_color=(0.86, 0.15, 0.15, 1),
                color=(1, 1, 1, 1),
                bold=True,
                font_size=sp(14),
            )
            self.start_btn.bind(on_release=self._on_start_batch)

            self.stop_btn = Button(
                text="Stop",
                size_hint_x=0.28,
                disabled=True,
                background_normal="",
                background_color=(0.45, 0.15, 0.15, 1),
                color=(1, 1, 1, 1),
                font_size=sp(14),
            )
            self.stop_btn.bind(on_release=self._on_stop_batch)

            action_bar.add_widget(self.start_btn)
            action_bar.add_widget(self.stop_btn)
            root.add_widget(action_bar)

            Clock.schedule_interval(self._poll_queue, 0.08)
            return root

        def _append_log(self, text: str) -> None:
            self.log_box.text += f"{text.strip()}\n"
            self.log_box.cursor = (0, len(self.log_box.text.splitlines()))

        def _on_ota_update(self, *_: Any) -> None:
            if self.is_downloading:
                self._append_log("[Notice] Stop active downloads before updating engine.")
                return
            self.ota_btn.disabled = True
            self.ota_btn.text = "Updating..."

            def _run() -> None:
                ok, msg = update_ytdlp_ota(
                    log_callback=lambda m: self.ui_queue.put(("log", m))
                )
                self.ui_queue.put(("ota_done", (ok, msg)))

            threading.Thread(target=_run, daemon=True).start()

        def _on_paste(self, *_: Any) -> None:
            try:
                clip = Clipboard.paste()
                if clip:
                    cur = self.url_input.text.strip()
                    self.url_input.text = (
                        f"{cur}\n{clip.strip()}\n" if cur else f"{clip.strip()}\n"
                    )
            except Exception as exc:
                self._append_log(f"[Clipboard Error] {exc}")

        def _on_add_urls(self, *_: Any) -> None:
            urls = extract_urls_from_text(self.url_input.text)
            if not urls:
                self._append_log(
                    "[Notice] Paste at least one valid https:// YouTube link first."
                )
                return

            expand_pl = bool(self.expand_pl_check.active)
            quality_label = self.quality_spinner.text
            self.url_input.text = ""

            # Expand playlist links in a background thread so UI never freezes
            def _expand_and_add() -> None:
                added = 0
                for url in urls:
                    if expand_pl and is_pure_playlist_url(url):
                        self.ui_queue.put(
                            ("log", f"[Playlist] Fetching video list from playlist...")
                        )
                        try:
                            pl_title, entries = expand_playlist_flat(url)
                            if entries:
                                total = len(entries)
                                for idx, (v_url, v_title) in enumerate(entries, start=1):
                                    self.ui_queue.put(
                                        (
                                            "add_task",
                                            (
                                                v_url,
                                                False,
                                                quality_label,
                                                v_title,
                                                pl_title,
                                                f"[{idx}/{total}]",
                                            ),
                                        )
                                    )
                                    added += 1
                                self.ui_queue.put(
                                    (
                                        "log",
                                        f"[Playlist] Expanded '{pl_title}' into {total} individual videos.",
                                    )
                                )
                                continue
                        except Exception as exc:
                            self.ui_queue.put(
                                ("log", f"[Playlist Warning] Flat expand failed ({exc}); queueing direct URL.")
                            )

                    self.ui_queue.put(
                        ("add_task", (url, False, quality_label, url, "", ""))
                    )
                    added += 1
                self.ui_queue.put(("log", f"[Queue] Ready ({added} video(s) added)."))

            threading.Thread(target=_expand_and_add, daemon=True).start()

        def _rebuild_queue_cards(self) -> None:
            self.queue_container.clear_widgets()
            for idx, tid in enumerate(self.task_order, start=1):
                t = self.tasks[tid]
                badge = f" {t.playlist_badge}" if t.playlist_badge else ""
                row = Label(
                    text=(
                        f"[b]#{idx}{badge}[/b] {t.title}\n"
                        f"Status: {t.status}  |  {t.progress_pct:.1f}%  |  Speed: {t.speed_str}  |  ETA: {t.eta_str}"
                    ),
                    markup=True,
                    size_hint_y=None,
                    height=dp(46),
                    font_size=sp(12),
                    color=(0.06, 0.09, 0.16, 1),
                    halign="left",
                    valign="middle",
                )
                row.bind(size=lambda inst, val: setattr(inst, "text_size", val))
                self.queue_container.add_widget(row)

            if self.task_order:
                avg = sum(
                    self.tasks[k].progress_pct for k in self.task_order
                ) / len(self.task_order)
                self.progress_bar.value = avg

        def _on_start_batch(self, *_: Any) -> None:
            raw_box = self.url_input.text.strip()
            if raw_box:
                urls = extract_urls_from_text(raw_box)
                self.url_input.text = ""
                expand_pl = bool(self.expand_pl_check.active)
                quality_label = self.quality_spinner.text
                self._start_with_urls_and_queue(urls, expand_pl, quality_label)
            else:
                self._start_with_urls_and_queue([], False, self.quality_spinner.text)

        def _start_with_urls_and_queue(
            self,
            new_urls: List[str],
            expand_pl: bool,
            quality_label: str,
        ) -> None:
            self.cancel_event.clear()
            self.is_downloading = True
            self.start_btn.disabled = True
            self.stop_btn.disabled = False

            def coordinator() -> None:
                # 1. Expand any newly pasted URLs (including playlists) first
                for url in new_urls:
                    if self.cancel_event.is_set():
                        break
                    if expand_pl and is_pure_playlist_url(url):
                        self.ui_queue.put(
                            ("log", "[Playlist] Expanding playlist items...")
                        )
                        try:
                            pl_title, entries = expand_playlist_flat(url)
                            if entries:
                                total = len(entries)
                                for idx, (v_url, v_title) in enumerate(entries, start=1):
                                    self.ui_queue.put(
                                        (
                                            "add_task",
                                            (
                                                v_url,
                                                False,
                                                quality_label,
                                                v_title,
                                                pl_title,
                                                f"[{idx}/{total}]",
                                            ),
                                        )
                                    )
                                self.ui_queue.put(
                                    (
                                        "log",
                                        f"[Playlist] Queued {total} videos from '{pl_title}'.",
                                    )
                                )
                                continue
                        except Exception:
                            pass
                    self.ui_queue.put(
                        ("add_task", (url, False, quality_label, url, "", ""))
                    )

                # Give UI queue a brief moment to register new tasks
                import time

                time.sleep(0.2)

                queued = [
                    tid
                    for tid in list(self.task_order)
                    if self.tasks[tid].status in ("Queued", "Failed", "Cancelled")
                ]
                if not queued:
                    self.ui_queue.put(("log", "[Notice] No queued items to download."))
                    self.ui_queue.put(("batch_done", None))
                    return

                self.ui_queue.put(
                    (
                        "log",
                        f"[Batch Start] Downloading {len(queued)} video(s) -> {self.output_dir}",
                    )
                )

                # Use 2 concurrent workers on Android so YouTube doesn't IP-rate-limit
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self._worker, tid) for tid in queued]
                    concurrent.futures.wait(futures)

                self.ui_queue.put(("batch_done", None))

            threading.Thread(target=coordinator, daemon=True).start()

        def _on_stop_batch(self, *_: Any) -> None:
            if self.is_downloading:
                self.cancel_event.set()
                self._append_log("[User Action] Stopping active downloads...")

        def _worker(self, tid: str) -> None:
            task = self.tasks[tid]
            if self.cancel_event.is_set():
                self.ui_queue.put(("status", (tid, "Cancelled", 0.0)))
                return

            self.ui_queue.put(("status", (tid, "Connecting...", 0.0)))
            finished_files: List[Path] = []

            def hook(d: Dict[str, Any]) -> None:
                if self.cancel_event.is_set():
                    raise DownloadCancelledError("Cancelled")
                st = d.get("status")
                info = d.get("info_dict") or {}
                title = info.get("title") or task.title

                if st == "downloading":
                    total = (
                        d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    )
                    dl = d.get("downloaded_bytes") or 0
                    pct = (dl / total * 100.0) if total > 0 else 0.0
                    spd = d.get("speed") or 0
                    spd_str = (
                        f"{spd / (1024 * 1024):.2f} MB/s" if spd > 0 else "—"
                    )
                    eta = d.get("eta")
                    eta_str = f"{int(eta)}s" if eta is not None else "—"
                    self.ui_queue.put(
                        (
                            "progress",
                            (
                                tid,
                                title,
                                pct,
                                spd_str,
                                eta_str,
                                task.playlist_badge,
                                "Downloading",
                            ),
                        )
                    )
                elif st == "finished":
                    fn = d.get("filename") or ""
                    if fn:
                        fp = Path(fn)
                        if fp not in finished_files:
                            finished_files.append(fp)
                    self.ui_queue.put(
                        (
                            "progress",
                            (
                                tid,
                                title,
                                100.0,
                                "—",
                                "0s",
                                task.playlist_badge,
                                "Saving MP4...",
                            ),
                        )
                    )

            last_err = "Download failed (0 files saved)."

            # Try each non-403 Android client profile (and auto-resume if a large file
            # on the `android` client hits the 30-second stream URL expiry)
            for profile_name, clients in ANDROID_CLIENT_PROFILES:
                if self.cancel_event.is_set():
                    self.ui_queue.put(("status", (tid, "Cancelled", 0.0)))
                    return

                # Allow up to 3 resume cycles on `android` client if a large file takes >30s
                for resume_cycle in range(2):
                    logger = YtDlpTaskLogger()
                    opts = build_mobile_ytdlp_options(
                        output_dir=self.staging_dir,
                        quality_label=task.quality_label,
                        expand_playlists=False,
                        progress_hook=hook,
                        logger=logger,
                        player_clients=clients,
                    )
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(task.url, download=True)
                            if isinstance(info, dict):
                                task.title = info.get("title") or task.title
                    except DownloadCancelledError:
                        self.ui_queue.put(("status", (tid, "Cancelled", 0.0)))
                        return
                    except Exception as exc:
                        err_s = clean_ansi(str(exc))
                        if err_s and err_s not in logger.errors:
                            logger.errors.append(err_s)

                    if finished_files:
                        # Export staged file(s) to public Android Downloads directory
                        saved_paths: List[Path] = []
                        for staged_fp in finished_files:
                            if staged_fp.exists():
                                out_fp = export_file_to_public_downloads(
                                    staged_fp,
                                    self.output_dir,
                                    subfolder=task.playlist_folder,
                                )
                                saved_paths.append(out_fp)
                        self.ui_queue.put(("status", (tid, "Completed", 100.0)))
                        self.ui_queue.put(
                            (
                                "log",
                                f"[Saved] {task.title} -> {saved_paths[-1].name if saved_paths else 'MP4'}",
                            )
                        )
                        return

                    if logger.errors:
                        last_err = logger.errors[-1]
                        # If a partial file is actively growing and hit 30s expiry, resume once
                        if "403" in last_err and resume_cycle == 0:
                            continue
                    break

                self.ui_queue.put(
                    (
                        "log",
                        f"[{tid}] Profile '{profile_name}' failed ({last_err}); trying next fallback...",
                    )
                )

            # If all profiles failed with 403 and we haven't run OTA update yet, auto-update yt-dlp once!
            if "403" in last_err and not self.did_auto_ota:
                self.did_auto_ota = True
                self.ui_queue.put(
                    (
                        "log",
                        f"[{tid}] 403 Forbidden detected — auto-updating yt-dlp engine from PyPI...",
                    )
                )
                ok, msg = update_ytdlp_ota(
                    log_callback=lambda m: self.ui_queue.put(("log", m))
                )
                if ok:
                    self.ui_queue.put(("ota_done", (True, msg)))
                    # Retry once with fresh yt-dlp
                    logger = YtDlpTaskLogger()
                    opts = build_mobile_ytdlp_options(
                        output_dir=self.staging_dir,
                        quality_label=task.quality_label,
                        expand_playlists=False,
                        progress_hook=hook,
                        logger=logger,
                        player_clients=["android", "web_embedded"],
                    )
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(task.url, download=True)
                            if isinstance(info, dict):
                                task.title = info.get("title") or task.title
                    except Exception as exc:
                        last_err = clean_ansi(str(exc))

                    if finished_files:
                        for staged_fp in finished_files:
                            if staged_fp.exists():
                                export_file_to_public_downloads(
                                    staged_fp,
                                    self.output_dir,
                                    subfolder=task.playlist_folder,
                                )
                        self.ui_queue.put(("status", (tid, "Completed", 100.0)))
                        self.ui_queue.put(("log", f"[Saved] {task.title}"))
                        return

            self.ui_queue.put(("status", (tid, "Failed", 0.0)))
            self.ui_queue.put(("log", f"[FAILED] {task.title} -> {last_err}"))

        def _poll_queue(self, _dt: float) -> None:
            dirty = False
            try:
                while True:
                    kind, payload = self.ui_queue.get_nowait()
                    if kind == "log":
                        self._append_log(str(payload))
                    elif kind == "ota_done":
                        ok, msg = payload
                        self.ota_btn.disabled = False
                        self.ota_btn.text = "↻ Update Engine"
                        ver_str = (
                            getattr(yt_dlp.version, "__version__", "updated")
                            if HAS_YTDLP
                            else "missing"
                        )
                        self.header_lbl.text = (
                            f"[b]YouTube Batch & Playlist Downloader[/b]\n"
                            f"[size=11sp]yt-dlp {ver_str}  •  H.264+AAC MP4 (Anti-403 Active)[/size]"
                        )
                        self._append_log(
                            f"[OTA Updater] {'SUCCESS' if ok else 'ERROR'}: {msg}"
                        )
                    elif kind == "add_task":
                        url, is_pl, qual, title, pl_folder, badge = payload
                        self._counter += 1
                        tid = f"item_{self._counter}"
                        self.tasks[tid] = MobileDownloadTask(
                            task_id=tid,
                            url=url,
                            is_playlist=is_pl,
                            quality_label=qual,
                            title=title,
                            playlist_folder=pl_folder,
                            playlist_badge=badge,
                        )
                        self.task_order.append(tid)
                        dirty = True
                    elif kind == "progress":
                        tid, title, pct, spd, eta, badge, st = payload
                        if tid in self.tasks:
                            t = self.tasks[tid]
                            t.title = title
                            t.progress_pct = pct
                            t.speed_str = spd
                            t.eta_str = eta
                            t.playlist_badge = badge
                            t.status = st
                            dirty = True
                    elif kind == "status":
                        tid, st, pct = payload
                        if tid in self.tasks:
                            self.tasks[tid].status = st
                            self.tasks[tid].progress_pct = pct
                            dirty = True
                    elif kind == "batch_done":
                        self.is_downloading = False
                        self.start_btn.disabled = False
                        self.stop_btn.disabled = True
                        done = sum(
                            1
                            for k in self.task_order
                            if self.tasks[k].status == "Completed"
                        )
                        failed = sum(
                            1
                            for k in self.task_order
                            if self.tasks[k].status == "Failed"
                        )
                        self._append_log(
                            f"[Batch Finished] {done} saved, {failed} failed. Folder: {self.output_dir}"
                        )
                        dirty = True
            except queue.Empty:
                pass

            if dirty:
                self._rebuild_queue_cards()

    YouTubeMobileApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_kivy_app())