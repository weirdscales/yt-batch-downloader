#!/usr/bin/env python3
"""
Batch YouTube Video & Playlist Downloader (Tkinter GUI)
=======================================================
A reliable desktop GUI application built with Tkinter and `yt-dlp` for
downloading multiple custom YouTube videos or entire playlists simultaneously.

Key Features & Fixes:
1. Universal H.264 (AVC) + AAC Playback (No Unsupported AV1 Video):
   - By default, `yt-dlp` ranks `av01` (AV1) above `avc1` (H.264) because both
     use `.mp4` containers, causing black screens or audio-only playback on
     Windows Media Player, Movies & TV, QuickTime, and older devices.
   - This app explicitly prioritizes H.264 (`vcodec^=avc` + `format_sort:
     vcodec:h264,acodec:aac`) so YouTube's native H.264 + AAC streams are
     downloaded and remuxed at full speed without AV1 compatibility issues.
   - If a rare video only exists in AV1 on YouTube, the app automatically
     converts the video stream to H.264 (`libx264` + `aac`) via `ffmpeg`.
2. No False "Completed" Status:
   - Tracks actual downloaded files on disk and captures all `yt-dlp` errors via
     `YtDlpTaskLogger`. If 0 files are saved, the row is marked `Failed`.
3. Automatic FFmpeg & Player Client Fallbacks:
   - Uses `imageio-ffmpeg` (or system `ffmpeg`) automatically and retries across
     fallback YouTube player clients (`ios`, `android_vr`, `tv`, `mweb`) if
     needed.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Tkinter & Optional Drag-and-Drop Support (tkinterdnd2)
# ---------------------------------------------------------------------------
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    HAS_TK = True
except ImportError:
    tk = None  # type: ignore
    filedialog = None  # type: ignore
    messagebox = None  # type: ignore
    ttk = None  # type: ignore
    HAS_TK = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    HAS_DND = True
except ImportError:
    DND_FILES = "DND_Files"
    TkinterDnD = None
    HAS_DND = False

# ---------------------------------------------------------------------------
# yt-dlp & imageio-ffmpeg Engine Checks
# ---------------------------------------------------------------------------
try:
    import yt_dlp  # type: ignore

    HAS_YTDLP = True
except ImportError:
    yt_dlp = None  # type: ignore
    HAS_YTDLP = False

try:
    import imageio_ffmpeg  # type: ignore

    HAS_IMAGEIO_FFMPEG = True
except ImportError:
    imageio_ffmpeg = None  # type: ignore
    HAS_IMAGEIO_FFMPEG = False


# ---------------------------------------------------------------------------
# Constants & Styling Palette
# ---------------------------------------------------------------------------
PALETTE = {
    "bg_app": "#F8FAFC",
    "bg_card": "#FFFFFF",
    "border": "#CBD5E1",
    "text_primary": "#0F172A",
    "text_secondary": "#475569",
    "text_muted": "#64748B",
    "accent": "#DC2626",
    "accent_hover": "#B91C1C",
    "blue": "#2563EB",
    "success": "#15803D",
    "warning": "#B45309",
    "danger": "#991B1B",
}

QUALITY_PRESETS: List[str] = [
    "Best Available (MP4 H.264)",
    "1080p Full HD (MP4 H.264)",
    "720p HD (MP4 H.264)",
    "480p SD (MP4 H.264)",
    "Audio Only (Best MP3/M4A)",
]

URL_REGEX = re.compile(r"https?://[^\s,;\"'<>]+", re.IGNORECASE)
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class DownloadCancelledError(Exception):
    """Raised inside a yt-dlp progress hook when the user cancels a download."""


def clean_ansi(text: str) -> str:
    """Strip ANSI color escape sequences from yt-dlp error strings."""
    return ANSI_ESCAPE_RE.sub("", text or "").strip()


class YtDlpTaskLogger:
    """
    Custom logger passed to `yt_dlp.YoutubeDL({'logger': ...})` so errors and
    warnings are captured per task instead of silently printing to stderr while
    the task pretends to succeed.
    """

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
class DownloadTask:
    """Represents a single URL entry (video or playlist) in the download queue."""

    task_id: str
    url: str
    is_playlist: bool
    quality_label: str
    avoid_av1: bool = True
    title: str = "Pending metadata..."
    status: str = "Queued"
    progress_pct: float = 0.0
    speed_str: str = "—"
    eta_str: str = "—"
    playlist_progress: str = ""
    error_message: str = ""
    output_dir: Path = field(default_factory=lambda: Path.home() / "Downloads")


# ---------------------------------------------------------------------------
# Pure Helper Functions (Testable in Headless Mode)
# ---------------------------------------------------------------------------
def resolve_ffmpeg_path() -> Optional[str]:
    """
    Locate a working `ffmpeg` binary from:
    1. The system PATH (`shutil.which('ffmpeg')`)
    2. The Python `imageio-ffmpeg` package (`imageio_ffmpeg.get_ffmpeg_exe()`)
    """
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    if HAS_IMAGEIO_FFMPEG and imageio_ffmpeg is not None:
        try:
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and Path(exe).is_file():
                return str(exe)
        except Exception:
            pass
    return None


def has_ffmpeg() -> bool:
    """Return True if ffmpeg is available via system PATH or imageio-ffmpeg."""
    return resolve_ffmpeg_path() is not None


def is_pure_playlist_url(url: str) -> bool:
    """
    Return True if the URL is a dedicated playlist link (`/playlist?list=...`)
    rather than an individual video watch link (`/watch?v=...`).
    """
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


def is_playlist_url(url: str, expand_watch_mixes: bool = False) -> bool:
    """
    Determine whether a URL should be treated as a multi-video playlist.
    By default, `/playlist?list=...` is always a playlist, while `/watch?v=...&list=...`
    is treated as a single video unless `expand_watch_mixes=True`.
    """
    if is_pure_playlist_url(url):
        return True
    if expand_watch_mixes:
        try:
            parsed = urlparse(url.strip())
            query = parse_qs(parsed.query)
            return bool(query.get("list"))
        except Exception:
            pass
    return False


def extract_urls_from_text(raw_text: str) -> List[str]:
    """
    Extract and deduplicate valid HTTP(S) URLs from free-form text, preserving order.
    Supports newline-separated, comma-separated, or space-separated links.
    """
    found = URL_REGEX.findall(raw_text or "")
    seen = set()
    unique_urls: List[str] = []
    for raw_url in found:
        cleaned = raw_url.rstrip(".,;)]}")
        if cleaned not in seen:
            seen.add(cleaned)
            unique_urls.append(cleaned)
    return unique_urls


def build_ytdlp_options(
    output_dir: Path,
    quality_label: str,
    expand_playlists: bool,
    playlist_subfolder: bool,
    avoid_av1: bool = True,
    ffmpeg_available: Optional[bool] = None,
    ffmpeg_location: Optional[str] = None,
    progress_hook: Optional[Any] = None,
    postprocessor_hook: Optional[Any] = None,
    logger: Optional[Any] = None,
    player_clients: Optional[List[str]] = None,
    unrestricted_fallback: bool = False,
) -> Dict[str, Any]:
    """
    Construct a resilient `yt-dlp` options dictionary that:
    1. Avoids `Requested format is not available` by falling back gracefully.
    2. Excludes unsupported `av01` (AV1) streams and prioritizes `h264` (`avc1`)
       video + `aac` (`mp4a`) audio so downloaded `.mp4` files play video and
       audio cleanly in every standard media player.
    """
    if ffmpeg_location is None:
        ffmpeg_location = resolve_ffmpeg_path()
    if ffmpeg_available is None:
        ffmpeg_available = ffmpeg_location is not None

    output_dir = Path(output_dir).expanduser().resolve()

    if expand_playlists and playlist_subfolder:
        outtmpl = str(
            output_dir
            / "%(playlist_title&{}/|)s%(playlist_index&{} - |)s%(title).180B [%(id)s].%(ext)s"
        )
    else:
        outtmpl = str(output_dir / "%(title).180B [%(id)s].%(ext)s")

    opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": not expand_playlists,
        "ignoreerrors": "only_download" if expand_playlists else False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    if logger is not None:
        opts["logger"] = logger

    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]

    if postprocessor_hook is not None:
        opts["postprocessor_hooks"] = [postprocessor_hook]

    if player_clients:
        opts["extractor_args"] = {"youtube": {"player_client": player_clients}}

    if "Audio Only" in quality_label:
        opts["format"] = "ba[acodec^=mp4a]/ba/b"
        opts["format_sort"] = ["acodec:aac", "aext:m4a:mp3"]
        if ffmpeg_available:
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        return opts

    # Video + Audio presets:
    # Why AV1 happened previously: YouTube serves both `av01` (AV1) and `avc1` (H.264)
    # inside `.mp4` containers (`vext=mp4`), and yt-dlp's default codec sort ranks
    # `av01` higher than `avc1`. By putting `vcodec:h264` and `acodec:aac` in
    # `format_sort` AND preferring `vcodec^=avc` / `vcodec!*=av01` in `format`,
    # yt-dlp always selects universally playable H.264 + AAC streams first!
    if unrestricted_fallback:
        opts["format"] = (
            "bv*[vcodec!*=av01]+ba/b[vcodec!*=av01]/bv*+ba/b"
            if ffmpeg_available
            else "b[vcodec!*=av01]/bv*[vcodec!*=av01]+ba/b/bv*+ba"
        )
    elif ffmpeg_available:
        if avoid_av1:
            opts["format"] = (
                "bv*[vcodec^=avc]+ba[acodec^=mp4a]/"
                "bv*[vcodec^=avc]+ba/"
                "bv*[vcodec!*=av01][vcodec!*=vp9]+ba/"
                "bv*[vcodec!*=av01]+ba/"
                "b[vcodec!*=av01]/"
                "bv*+ba/b"
            )
        else:
            opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = "mp4"
    else:
        if avoid_av1:
            opts["format"] = "b[vcodec^=avc]/b[vcodec!*=av01]/bv*[vcodec^=avc]+ba/b/bv*+ba"
        else:
            opts["format"] = "b/bv*+ba"

    # Build format_sort (-S) with explicit H.264 + AAC priority
    sort_rules: List[str] = []
    if "1080p" in quality_label:
        sort_rules.append("res:1080")
    elif "720p" in quality_label:
        sort_rules.append("res:720")
    elif "480p" in quality_label:
        sort_rules.append("res:480")

    if avoid_av1:
        # Rank H.264 (avc1) video and AAC (mp4a) audio at the top of codec preferences
        sort_rules.extend(["vcodec:h264", "acodec:aac", "vext:mp4", "aext:m4a"])
    else:
        sort_rules.extend(["vext:mp4", "aext:m4a"])

    opts["format_sort"] = sort_rules
    return opts


def transcode_av1_to_h264_if_needed(
    file_path: Path,
    vcodec: str,
    ffmpeg_location: Optional[str],
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """
    Safety net: If a rare YouTube video had NO native H.264 stream on YouTube's
    servers and `yt-dlp` was forced to fall back to `av01` or `vp9`, automatically
    re-encode the video track to H.264 (`libx264` + `aac`) so it plays everywhere.
    Returns True if transcoding was performed.
    """
    vcodec_lower = (vcodec or "").lower()
    if not vcodec_lower or vcodec_lower.startswith(("avc", "h264", "none")):
        return False
    if not ("av01" in vcodec_lower or "av1" in vcodec_lower or "vp9" in vcodec_lower or "vp09" in vcodec_lower):
        return False
    if not ffmpeg_location or not file_path.is_file():
        return False
    if cancel_event and cancel_event.is_set():
        return False

    temp_h264 = file_path.with_suffix(".h264_temp.mp4")
    cmd = [
        ffmpeg_location,
        "-y",
        "-i",
        str(file_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temp_h264),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and temp_h264.is_file() and temp_h264.stat().st_size > 0:
            temp_h264.replace(file_path.with_suffix(".mp4"))
            if file_path.suffix.lower() != ".mp4" and file_path.exists():
                file_path.unlink(missing_ok=True)
            return True
    except Exception:
        pass
    finally:
        if temp_h264.exists():
            try:
                temp_h264.unlink()
            except OSError:
                pass
    return False


def format_speed(speed_bps: Optional[float]) -> str:
    """Format download speed in human-readable B/s, KB/s, or MB/s."""
    if not speed_bps or speed_bps <= 0:
        return "—"
    if speed_bps >= 1024 * 1024:
        return f"{speed_bps / (1024 * 1024):.2f} MB/s"
    if speed_bps >= 1024:
        return f"{speed_bps / 1024:.1f} KB/s"
    return f"{int(speed_bps)} B/s"


def format_eta(seconds: Optional[float]) -> str:
    """Format ETA seconds into mm:ss or hh:mm:ss."""
    if seconds is None or seconds < 0:
        return "—"
    sec = int(seconds)
    mins, s = divmod(sec, 60)
    hrs, m = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Main Tkinter Application
# ---------------------------------------------------------------------------
class YouTubeBatchDownloaderApp:
    """Tkinter GUI Controller for parallel multi-video and playlist downloading."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.root.title("Batch YouTube Video & Playlist Downloader (H.264 MP4)")
        self.root.geometry("1040x760")
        self.root.minsize(900, 640)
        self.root.configure(bg=PALETTE["bg_app"])

        # Application state
        self.tasks: Dict[str, DownloadTask] = {}
        self.task_order: List[str] = []
        self._task_counter = 0

        default_dl = Path.home() / "Downloads" / "YouTube_Downloads"
        self.output_dir_var = tk.StringVar(value=str(default_dl))
        self.quality_var = tk.StringVar(value=QUALITY_PRESETS[0])
        self.workers_var = tk.IntVar(value=3)
        self.avoid_av1_var = tk.BooleanVar(value=True)
        self.expand_playlists_var = tk.BooleanVar(value=True)
        self.expand_watch_mix_var = tk.BooleanVar(value=False)
        self.playlist_subfolder_var = tk.BooleanVar(value=True)
        self.overall_progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(
            value="Paste YouTube video or playlist links above and click '+ Add URLs to Queue'."
        )

        # Thread synchronization
        self.ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.is_downloading = False

        self._configure_styles()
        self._build_ui()

        # Periodic UI queue poller
        self.root.after(60, self._poll_ui_queue)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TButton", font=("Segoe UI", 9), padding=(10, 5))
        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 10, "bold"),
            foreground="#FFFFFF",
            background=PALETTE["accent"],
            padding=(16, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", PALETTE["accent_hover"]), ("disabled", "#94A3B8")],
            foreground=[("disabled", "#E2E8F0")],
        )
        style.configure(
            "Blue.TButton",
            font=("Segoe UI", 9, "bold"),
            foreground="#FFFFFF",
            background=PALETTE["blue"],
            padding=(12, 6),
        )
        style.map(
            "Blue.TButton",
            background=[("active", "#1D4ED8"), ("disabled", "#94A3B8")],
        )
        style.configure(
            "Treeview",
            font=("Segoe UI", 9),
            rowheight=26,
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
            padding=(6, 5),
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background=PALETTE["accent"],
            thickness=14,
        )

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=3)
        self.root.rowconfigure(3, weight=1)

        # -------------------------------------------------------------------
        # 1. Header Bar + One-Click Engine Updater
        # -------------------------------------------------------------------
        header = tk.Frame(self.root, bg=PALETTE["bg_app"], padx=20, pady=10)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="Batch YouTube Video & Playlist Downloader",
            font=("Segoe UI", 16, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_app"],
        ).grid(row=0, column=0, sticky="w")

        self.update_engine_btn = ttk.Button(
            header,
            text="↻ Update yt-dlp & FFmpeg Engine",
            command=self._update_ytdlp_engine,
        )
        self.update_engine_btn.grid(row=0, column=1, rowspan=2, sticky="e")

        self.engine_status_lbl = tk.Label(
            header,
            text=self._get_engine_status_text(),
            font=("Segoe UI", 9),
            fg=PALETTE["text_secondary"] if has_ffmpeg() else PALETTE["warning"],
            bg=PALETTE["bg_app"],
        )
        self.engine_status_lbl.grid(row=1, column=0, sticky="w", pady=(2, 0))

        # -------------------------------------------------------------------
        # 2. URL Input & Settings Card
        # -------------------------------------------------------------------
        input_card = tk.Frame(
            self.root,
            bg=PALETTE["bg_card"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=16,
            pady=10,
        )
        input_card.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        input_card.columnconfigure(0, weight=1)

        top_input_bar = tk.Frame(input_card, bg=PALETTE["bg_card"])
        top_input_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top_input_bar.columnconfigure(0, weight=1)

        tk.Label(
            top_input_bar,
            text="Paste Video or Playlist URLs (one per line, or multiple links separated by spaces):",
            font=("Segoe UI", 10, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_card"],
        ).grid(row=0, column=0, sticky="w")

        btn_strip = tk.Frame(top_input_bar, bg=PALETTE["bg_card"])
        btn_strip.grid(row=0, column=1, sticky="e")

        ttk.Button(
            btn_strip,
            text="Paste Clipboard",
            command=self._paste_from_clipboard,
        ).pack(side="left", padx=(0, 6))

        ttk.Button(
            btn_strip,
            text="Load .txt File...",
            command=self._load_urls_from_file,
        ).pack(side="left", padx=(0, 6))

        ttk.Button(
            btn_strip,
            text="Clear Box",
            command=lambda: self.url_text.delete("1.0", "end"),
        ).pack(side="left")

        text_frame = tk.Frame(input_card, bg=PALETTE["bg_card"])
        text_frame.grid(row=1, column=0, sticky="ew")
        text_frame.columnconfigure(0, weight=1)

        self.url_text = tk.Text(
            text_frame,
            height=4,
            font=("Consolas", 10),
            relief="solid",
            bd=1,
            padx=8,
            pady=6,
            wrap="word",
        )
        self.url_text.grid(row=0, column=0, sticky="ew")

        text_scroll = ttk.Scrollbar(
            text_frame, orient="vertical", command=self.url_text.yview
        )
        text_scroll.grid(row=0, column=1, sticky="ns")
        self.url_text.configure(yscrollcommand=text_scroll.set)

        if HAS_DND:
            try:
                self.url_text.drop_target_register(DND_FILES)
                self.url_text.dnd_bind("<<Drop>>", self._on_drop_onto_text)
            except Exception:
                pass

        # Settings Row inside Input Card
        opts_row = tk.Frame(input_card, bg=PALETTE["bg_card"], pady=6)
        opts_row.grid(row=2, column=0, sticky="ew")

        tk.Label(
            opts_row,
            text="Format:",
            font=("Segoe UI", 9, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_card"],
        ).pack(side="left", padx=(0, 6))

        self.quality_combo = ttk.Combobox(
            opts_row,
            textvariable=self.quality_var,
            values=QUALITY_PRESETS,
            state="readonly",
            width=25,
            font=("Segoe UI", 9),
        )
        self.quality_combo.pack(side="left", padx=(0, 12))

        tk.Label(
            opts_row,
            text="Workers:",
            font=("Segoe UI", 9, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_card"],
        ).pack(side="left", padx=(0, 4))

        self.workers_spin = ttk.Spinbox(
            opts_row,
            from_=1,
            to=6,
            textvariable=self.workers_var,
            width=3,
            font=("Segoe UI", 9),
        )
        self.workers_spin.pack(side="left", padx=(0, 12))

        ttk.Checkbutton(
            opts_row,
            text="Force H.264 + AAC (No AV1)",
            variable=self.avoid_av1_var,
        ).pack(side="left", padx=(0, 10))

        ttk.Checkbutton(
            opts_row,
            text="Download Playlists",
            variable=self.expand_playlists_var,
        ).pack(side="left", padx=(0, 10))

        ttk.Checkbutton(
            opts_row,
            text="Expand watch?v=...&list=",
            variable=self.expand_watch_mix_var,
        ).pack(side="left", padx=(0, 10))

        ttk.Checkbutton(
            opts_row,
            text="Playlist Subfolders",
            variable=self.playlist_subfolder_var,
        ).pack(side="left")

        self.add_queue_btn = ttk.Button(
            opts_row,
            text="+ Add URLs to Queue",
            style="Blue.TButton",
            command=self._add_input_urls_to_queue,
        )
        self.add_queue_btn.pack(side="right")

        # -------------------------------------------------------------------
        # 3. Queue Table Card (Treeview)
        # -------------------------------------------------------------------
        queue_card = tk.Frame(
            self.root,
            bg=PALETTE["bg_card"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=16,
            pady=10,
        )
        queue_card.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 8))
        queue_card.columnconfigure(0, weight=1)
        queue_card.rowconfigure(1, weight=1)

        queue_header = tk.Frame(queue_card, bg=PALETTE["bg_card"])
        queue_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        queue_header.columnconfigure(0, weight=1)

        self.queue_count_lbl = tk.Label(
            queue_header,
            text="Download Queue (0 items)",
            font=("Segoe UI", 10, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_card"],
        )
        self.queue_count_lbl.grid(row=0, column=0, sticky="w")

        q_btns = tk.Frame(queue_header, bg=PALETTE["bg_card"])
        q_btns.grid(row=0, column=1, sticky="e")

        ttk.Button(
            q_btns,
            text="Retry Failed / Cancelled",
            command=self._retry_failed_tasks,
        ).pack(side="left", padx=(0, 6))

        ttk.Button(
            q_btns,
            text="Remove Selected",
            command=self._remove_selected_tasks,
        ).pack(side="left", padx=(0, 6))

        ttk.Button(
            q_btns,
            text="Clear Completed",
            command=self._clear_completed_tasks,
        ).pack(side="left")

        columns = ("idx", "type", "title", "quality", "status", "progress", "speed", "eta")
        self.tree = ttk.Treeview(
            queue_card,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=8,
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("type", text="Type")
        self.tree.heading("title", text="Video / Playlist Title or URL")
        self.tree.heading("quality", text="Format")
        self.tree.heading("status", text="Status")
        self.tree.heading("progress", text="Progress")
        self.tree.heading("speed", text="Speed")
        self.tree.heading("eta", text="ETA")

        self.tree.column("idx", width=40, anchor="center", stretch=False)
        self.tree.column("type", width=80, anchor="center", stretch=False)
        self.tree.column("title", width=330, anchor="w", stretch=True)
        self.tree.column("quality", width=165, anchor="w", stretch=False)
        self.tree.column("status", width=150, anchor="w", stretch=False)
        self.tree.column("progress", width=90, anchor="center", stretch=False)
        self.tree.column("speed", width=90, anchor="e", stretch=False)
        self.tree.column("eta", width=65, anchor="center", stretch=False)

        self.tree.grid(row=1, column=0, sticky="nsew")

        tree_scroll = ttk.Scrollbar(
            queue_card, orient="vertical", command=self.tree.yview
        )
        tree_scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        # -------------------------------------------------------------------
        # 4. Live Activity & Error Log Card
        # -------------------------------------------------------------------
        log_card = tk.Frame(
            self.root,
            bg=PALETTE["bg_card"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=16,
            pady=8,
        )
        log_card.grid(row=3, column=0, sticky="nsew", padx=20, pady=(0, 8))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)

        log_header = tk.Frame(log_card, bg=PALETTE["bg_card"])
        log_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        log_header.columnconfigure(0, weight=1)

        tk.Label(
            log_header,
            text="Live Activity & Error Log:",
            font=("Segoe UI", 9, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_card"],
        ).grid(row=0, column=0, sticky="w")

        ttk.Button(
            log_header,
            text="Clear Log",
            command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

        self.log_text = tk.Text(
            log_card,
            height=4,
            font=("Consolas", 9),
            bg="#0F172A",
            fg="#E2E8F0",
            relief="flat",
            padx=8,
            pady=6,
            wrap="word",
            state="disabled",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")

        log_scroll = ttk.Scrollbar(
            log_card, orient="vertical", command=self.log_text.yview
        )
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        # -------------------------------------------------------------------
        # 5. Output Folder & Start/Cancel Footer
        # -------------------------------------------------------------------
        footer = tk.Frame(self.root, bg=PALETTE["bg_app"], padx=20, pady=8)
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)

        tk.Label(
            footer,
            text="Save To Folder:",
            font=("Segoe UI", 10, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_app"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.out_entry = ttk.Entry(
            footer,
            textvariable=self.output_dir_var,
            font=("Segoe UI", 10),
        )
        self.out_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), ipady=3)

        ttk.Button(
            footer,
            text="Browse Folder...",
            command=self._browse_output_dir,
        ).grid(row=0, column=2, sticky="e")

        self.progress_bar = ttk.Progressbar(
            footer,
            variable=self.overall_progress_var,
            maximum=100.0,
            mode="determinate",
            style="Horizontal.TProgressbar",
        )
        self.progress_bar.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(8, 6)
        )

        bottom_row = tk.Frame(footer, bg=PALETTE["bg_app"])
        bottom_row.grid(row=2, column=0, columnspan=3, sticky="ew")
        bottom_row.columnconfigure(0, weight=1)

        tk.Label(
            bottom_row,
            textvariable=self.status_var,
            font=("Segoe UI", 9),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_app"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 12))

        self.cancel_btn = ttk.Button(
            bottom_row,
            text="Stop / Cancel All",
            command=self._cancel_all_downloads,
        )
        self.cancel_btn.grid(row=0, column=1, padx=(0, 8))
        self.cancel_btn.state(["disabled"])

        self.start_btn = ttk.Button(
            bottom_row,
            text="Download All Queued Videos",
            style="Accent.TButton",
            command=self._start_batch_download,
        )
        self.start_btn.grid(row=0, column=2, sticky="e")

    def _get_engine_status_text(self) -> str:
        ytdlp_ver = getattr(yt_dlp.version, "__version__", "installed") if HAS_YTDLP else "NOT INSTALLED"
        ffmpeg_path = resolve_ffmpeg_path()
        if ffmpeg_path:
            source = "System PATH" if shutil.which("ffmpeg") else "imageio-ffmpeg"
            ffmpeg_str = f"FFmpeg Active ({source})"
        else:
            ffmpeg_str = "FFmpeg Missing (Click 'Update yt-dlp & FFmpeg Engine' to install)"
        return f"yt-dlp {ytdlp_ver}  •  {ffmpeg_str}  •  Codec Mode: H.264 (AVC1) + AAC"

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message.strip()}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -----------------------------------------------------------------------
    # Self-Update Engine (`yt-dlp[default]` + `imageio-ffmpeg`)
    # -----------------------------------------------------------------------
    def _update_ytdlp_engine(self) -> None:
        if self.is_downloading:
            messagebox.showinfo(
                "Download Active",
                "Please wait for current downloads to finish before updating the engine.",
                parent=self.root,
            )
            return

        self.update_engine_btn.state(["disabled"])
        self.status_var.set("Updating yt-dlp[default] and imageio-ffmpeg via pip...")
        self._append_log(
            "[Engine Updater] Running: python -m pip install -U --pre yt-dlp[default] imageio-ffmpeg ..."
        )

        def run_pip() -> None:
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "-U",
                        "--pre",
                        "yt-dlp[default]",
                        "imageio-ffmpeg",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.ui_queue.put(("pip_update_done", (proc.returncode, proc.stdout, proc.stderr)))
            except Exception as exc:
                self.ui_queue.put(("pip_update_done", (1, "", str(exc))))

        threading.Thread(target=run_pip, daemon=True).start()

    # -----------------------------------------------------------------------
    # Queue & Input Handlers
    # -----------------------------------------------------------------------
    def _paste_from_clipboard(self) -> None:
        try:
            clip = self.root.clipboard_get()
        except Exception:
            return
        if clip:
            current = self.url_text.get("1.0", "end").strip()
            prefix = "\n" if current else ""
            self.url_text.insert("end", f"{prefix}{clip.strip()}\n")

    def _load_urls_from_file(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Select a Text File Containing YouTube URLs",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not chosen:
            return
        self._read_file_into_textbox(Path(chosen))

    def _on_drop_onto_text(self, event: Any) -> str:
        raw_data = getattr(event, "data", "")
        if not raw_data:
            return "break"
        for item in self.root.tk.splitlist(raw_data):
            p = Path(item)
            if p.is_file():
                self._read_file_into_textbox(p)
            else:
                self.url_text.insert("end", f"{item}\n")
        return "break"

    def _read_file_into_textbox(self, path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            self.url_text.insert("end", f"\n{content.strip()}\n")
        except Exception as exc:
            messagebox.showerror("Could Not Read File", str(exc), parent=self.root)

    def _browse_output_dir(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Choose Download Destination Folder",
            initialdir=self.output_dir_var.get() or str(Path.home()),
        )
        if chosen:
            self.output_dir_var.set(chosen)

    def _add_input_urls_to_queue(self) -> None:
        raw_text = self.url_text.get("1.0", "end")
        urls = extract_urls_from_text(raw_text)
        if not urls:
            messagebox.showwarning(
                "No URLs Found",
                "Please paste at least one valid http:// or https:// video or playlist URL.",
                parent=self.root,
            )
            return

        existing_urls = {
            t.url for t in self.tasks.values() if t.status in ("Queued", "Downloading", "Starting...")
        }
        seen_playlist_ids = set()
        expand_watch_mix = bool(self.expand_watch_mix_var.get())
        expand_playlists = bool(self.expand_playlists_var.get())
        avoid_av1 = bool(self.avoid_av1_var.get())

        added = 0
        for url in urls:
            if url in existing_urls:
                continue

            playlist_flag = expand_playlists and is_playlist_url(
                url, expand_watch_mixes=expand_watch_mix
            )

            if playlist_flag:
                try:
                    pl_id = parse_qs(urlparse(url).query).get("list", [""])[0]
                    if pl_id and pl_id in seen_playlist_ids:
                        continue
                    if pl_id:
                        seen_playlist_ids.add(pl_id)
                except Exception:
                    pass

            self._task_counter += 1
            task_id = f"task_{self._task_counter}"
            task = DownloadTask(
                task_id=task_id,
                url=url,
                is_playlist=playlist_flag,
                quality_label=self.quality_var.get(),
                avoid_av1=avoid_av1,
                title=url,
                output_dir=Path(self.output_dir_var.get().strip() or "."),
            )
            self.tasks[task_id] = task
            self.task_order.append(task_id)
            existing_urls.add(url)
            added += 1

        self.url_text.delete("1.0", "end")
        self._refresh_tree()
        self.status_var.set(
            f"Added {added} item{'s' if added != 1 else ''} to queue. "
            "Click 'Download All Queued Videos' to begin."
        )

    def _refresh_tree(self) -> None:
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())

        for idx, task_id in enumerate(self.task_order, start=1):
            t = self.tasks[task_id]
            kind_str = "Playlist" if t.is_playlist else "Video"
            prog_str = (
                f"{t.playlist_progress} ({t.progress_pct:.1f}%)"
                if t.playlist_progress
                else f"{t.progress_pct:.1f}%"
            )
            self.tree.insert(
                "",
                "end",
                iid=task_id,
                values=(
                    idx,
                    kind_str,
                    t.title,
                    t.quality_label,
                    t.status,
                    prog_str,
                    t.speed_str,
                    t.eta_str,
                ),
            )

        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)

        total = len(self.task_order)
        done = sum(1 for tid in self.task_order if self.tasks[tid].status == "Completed")
        failed = sum(
            1 for tid in self.task_order if self.tasks[tid].status.startswith("Failed")
        )
        self.queue_count_lbl.configure(
            text=(
                f"Download Queue ({total} item{'s' if total != 1 else ''}  •  "
                f"{done} completed  •  {failed} failed)"
            )
        )

    def _update_single_row(self, task_id: str) -> None:
        if not self.tree.exists(task_id) or task_id not in self.tasks:
            return
        t = self.tasks[task_id]
        idx = self.task_order.index(task_id) + 1
        kind_str = "Playlist" if t.is_playlist else "Video"
        prog_str = (
            f"{t.playlist_progress} ({t.progress_pct:.1f}%)"
            if t.playlist_progress
            else f"{t.progress_pct:.1f}%"
        )
        self.tree.item(
            task_id,
            values=(
                idx,
                kind_str,
                t.title,
                t.quality_label,
                t.status,
                prog_str,
                t.speed_str,
                t.eta_str,
            ),
        )
        self._update_overall_progress()

    def _update_overall_progress(self) -> None:
        if not self.task_order:
            self.overall_progress_var.set(0.0)
            return
        total_pct = sum(self.tasks[tid].progress_pct for tid in self.task_order)
        self.overall_progress_var.set(total_pct / len(self.task_order))

    def _remove_selected_tasks(self) -> None:
        if self.is_downloading:
            messagebox.showinfo(
                "Batch in Progress",
                "Please wait for active downloads to finish or click 'Stop / Cancel All' first.",
                parent=self.root,
            )
            return
        for tid in self.tree.selection():
            self.tasks.pop(tid, None)
            if tid in self.task_order:
                self.task_order.remove(tid)
        self._refresh_tree()
        self._update_overall_progress()

    def _clear_completed_tasks(self) -> None:
        to_remove = [
            tid for tid in self.task_order if self.tasks[tid].status == "Completed"
        ]
        for tid in to_remove:
            self.tasks.pop(tid, None)
            self.task_order.remove(tid)
        self._refresh_tree()
        self._update_overall_progress()

    def _retry_failed_tasks(self) -> None:
        retried = 0
        for tid in self.task_order:
            t = self.tasks[tid]
            if (
                t.status.startswith("Failed")
                or t.status.startswith("Partial")
                or t.status == "Cancelled"
            ):
                t.status = "Queued"
                t.avoid_av1 = bool(self.avoid_av1_var.get())
                t.quality_label = self.quality_var.get()
                t.progress_pct = 0.0
                t.speed_str = "—"
                t.eta_str = "—"
                t.error_message = ""
                retried += 1
        self._refresh_tree()
        if retried > 0 and not self.is_downloading:
            self._start_batch_download()

    # -----------------------------------------------------------------------
    # Parallel Batch Download Execution with H.264 Priority & Fallback
    # -----------------------------------------------------------------------
    def _start_batch_download(self) -> None:
        leftover_text = self.url_text.get("1.0", "end").strip()
        if leftover_text:
            self._add_input_urls_to_queue()

        queued_ids = [
            tid for tid in self.task_order if self.tasks[tid].status == "Queued"
        ]
        if not queued_ids:
            messagebox.showinfo(
                "Nothing Queued",
                "There are no queued items to download. Paste video or playlist links above first.",
                parent=self.root,
            )
            return

        if not HAS_YTDLP:
            messagebox.showerror(
                "Missing Dependency: yt-dlp",
                "The 'yt-dlp' library is not installed.\n\nRun:\n    pip install -r requirements.txt",
                parent=self.root,
            )
            return

        out_dir = Path(self.output_dir_var.get().strip() or ".").expanduser().resolve()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(
                "Invalid Output Folder",
                f"Cannot create output directory:\n{out_dir}\n\n{exc}",
                parent=self.root,
            )
            return

        self.cancel_event.clear()
        self.is_downloading = True
        self.start_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])

        max_workers = max(1, min(6, int(self.workers_var.get() or 3)))
        playlist_subfolder = bool(self.playlist_subfolder_var.get())

        self.status_var.set(
            f"Downloading {len(queued_ids)} item(s) with {max_workers} parallel worker(s)..."
        )
        self._append_log(
            f"[Batch Start] Starting {len(queued_ids)} item(s) (H.264/AAC mode={self.avoid_av1_var.get()}) -> {out_dir}"
        )

        def coordinator() -> None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(
                        self._download_single_task,
                        tid,
                        out_dir,
                        playlist_subfolder,
                    )
                    for tid in queued_ids
                ]
                concurrent.futures.wait(futures)
            self.ui_queue.put(("batch_finished", None))

        threading.Thread(target=coordinator, daemon=True).start()

    def _download_single_task(
        self,
        task_id: str,
        output_dir: Path,
        playlist_subfolder: bool,
    ) -> None:
        task = self.tasks[task_id]
        if self.cancel_event.is_set():
            self.ui_queue.put(("task_cancelled", task_id))
            return

        self.ui_queue.put(("task_started", task_id))

        finished_files: List[str] = []
        postprocessed_files: List[Tuple[Path, str]] = []

        def progress_hook(d: Dict[str, Any]) -> None:
            if self.cancel_event.is_set():
                raise DownloadCancelledError("Cancelled by user.")

            status = d.get("status")
            info = d.get("info_dict") or {}
            title = info.get("playlist_title") or info.get("title")
            playlist_idx = info.get("playlist_index")
            playlist_count = info.get("n_entries") or info.get("playlist_count")

            pl_badge = ""
            if playlist_idx and playlist_count:
                pl_badge = f"[{playlist_idx}/{playlist_count}]"

            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total * 100.0) if total > 0 else 0.0
                speed_str = format_speed(d.get("speed"))
                eta_str = format_eta(d.get("eta"))

                self.ui_queue.put(
                    (
                        "task_progress",
                        (task_id, title, pct, speed_str, eta_str, pl_badge, "Downloading"),
                    )
                )
            elif status == "finished":
                filename = d.get("filename") or ""
                if filename and filename not in finished_files:
                    finished_files.append(filename)
                self.ui_queue.put(
                    (
                        "task_progress",
                        (task_id, title, 100.0, "—", "00:00", pl_badge, "Merging MP4..."),
                    )
                )

        def postprocessor_hook(d: Dict[str, Any]) -> None:
            if d.get("status") == "finished":
                info = d.get("info_dict") or {}
                filepath = info.get("filepath") or info.get("_filename")
                vcodec = str(info.get("vcodec") or "")
                if filepath:
                    postprocessed_files.append((Path(filepath), vcodec))

        attempts: List[Tuple[str, Optional[List[str]], bool]] = [
            ("Default Engine (H.264 Priority)", None, False),
            ("Mobile/TV Fallback Client (H.264 Priority)", ["ios", "android_vr", "tv", "mweb"], False),
            ("Unrestricted Format Fallback", ["ios", "android_vr", "tv", "web"], True),
        ]

        last_errors: List[str] = []
        final_title = task.title
        ffmpeg_loc = resolve_ffmpeg_path()

        for attempt_idx, (label, clients, unrestricted) in enumerate(attempts, start=1):
            if self.cancel_event.is_set():
                self.ui_queue.put(("task_cancelled", task_id))
                return

            task_logger = YtDlpTaskLogger(
                log_callback=lambda msg, tid=task_id: self.ui_queue.put(
                    ("log_line", f"[{tid}] {msg}")
                )
            )

            opts = build_ytdlp_options(
                output_dir=output_dir,
                quality_label=task.quality_label,
                expand_playlists=task.is_playlist,
                playlist_subfolder=playlist_subfolder,
                avoid_av1=task.avoid_av1,
                ffmpeg_location=ffmpeg_loc,
                progress_hook=progress_hook,
                postprocessor_hook=postprocessor_hook,
                logger=task_logger,
                player_clients=clients,
                unrestricted_fallback=unrestricted,
            )

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(task.url, download=True)
                    if isinstance(info, dict):
                        final_title = (
                            info.get("playlist_title")
                            or info.get("title")
                            or final_title
                        )
            except DownloadCancelledError:
                self.ui_queue.put(("task_cancelled", task_id))
                return
            except Exception as exc:
                if self.cancel_event.is_set():
                    self.ui_queue.put(("task_cancelled", task_id))
                    return
                err_str = clean_ansi(str(exc))
                if err_str and err_str not in task_logger.errors:
                    task_logger.errors.append(err_str)

            if finished_files:
                # Safety check: if a fallback stream used AV1/VP9 and avoid_av1 is True,
                # automatically transcode the video track to H.264 so it never plays audio-only.
                if task.avoid_av1 and ffmpeg_loc and "Audio Only" not in task.quality_label:
                    for fp, vc in postprocessed_files:
                        if vc and ("av01" in vc.lower() or "vp9" in vc.lower() or "vp09" in vc.lower()):
                            self.ui_queue.put(
                                (
                                    "task_progress",
                                    (task_id, final_title, 99.0, "—", "—", "", "Converting to H.264..."),
                                )
                            )
                            transcode_av1_to_h264_if_needed(
                                fp, vc, ffmpeg_loc, self.cancel_event
                            )

                if task_logger.errors and task.is_playlist:
                    self.ui_queue.put(
                        (
                            "task_partial",
                            (
                                task_id,
                                final_title,
                                len(finished_files),
                                len(task_logger.errors),
                                task_logger.errors[-1],
                            ),
                        )
                    )
                else:
                    self.ui_queue.put(
                        ("task_completed", (task_id, final_title, len(finished_files)))
                    )
                return

            last_errors = task_logger.errors or ["No playable video stream was downloaded."]
            combined_err = " ".join(last_errors).lower()
            if attempt_idx < len(attempts) and (
                "requested format is not available" in combined_err
                or "sign in" in combined_err
                or "nsig" in combined_err
                or "sabr" in combined_err
            ):
                self.ui_queue.put(
                    (
                        "log_line",
                        f"[{task_id}] Attempt {attempt_idx} ({label}) failed; retrying with {attempts[attempt_idx][0]}...",
                    )
                )
                continue
            break

        primary_err = last_errors[-1] if last_errors else "Download failed (0 files saved)."
        self.ui_queue.put(("task_failed", (task_id, primary_err)))

    def _cancel_all_downloads(self) -> None:
        if self.is_downloading:
            self.cancel_event.set()
            self.status_var.set("Stopping active downloads...")
            self._append_log("[User Action] Cancelling active downloads...")

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                event_type, payload = self.ui_queue.get_nowait()
                if event_type == "log_line":
                    self._append_log(str(payload))
                elif event_type == "pip_update_done":
                    code, stdout, stderr = payload
                    self.update_engine_btn.state(["!disabled"])
                    if code == 0:
                        global HAS_YTDLP, yt_dlp, HAS_IMAGEIO_FFMPEG, imageio_ffmpeg
                        try:
                            if yt_dlp is not None:
                                importlib.reload(yt_dlp)
                            else:
                                import yt_dlp as _yt  # type: ignore

                                yt_dlp = _yt
                                HAS_YTDLP = True
                        except Exception:
                            pass
                        try:
                            import imageio_ffmpeg as _iff  # type: ignore

                            imageio_ffmpeg = _iff
                            HAS_IMAGEIO_FFMPEG = True
                        except Exception:
                            pass
                        self.engine_status_lbl.configure(
                            text=self._get_engine_status_text(),
                            fg=PALETTE["success"],
                        )
                        self._append_log(
                            "[Engine Updater] Successfully updated yt-dlp[default] and imageio-ffmpeg!"
                        )
                        self.status_var.set(
                            "Engine updated! Click 'Retry Failed / Cancelled' to download."
                        )
                    else:
                        self._append_log(
                            f"[Engine Updater Error] {stderr or stdout}"
                        )
                        self.status_var.set("Engine update failed (see log panel).")
                elif event_type == "task_started":
                    tid = payload
                    if tid in self.tasks:
                        self.tasks[tid].status = "Starting..."
                        self._update_single_row(tid)
                elif event_type == "task_progress":
                    tid, title, pct, speed_str, eta_str, pl_badge, status_text = payload
                    if tid in self.tasks:
                        t = self.tasks[tid]
                        if title:
                            t.title = title
                        t.progress_pct = pct
                        t.speed_str = speed_str
                        t.eta_str = eta_str
                        t.playlist_progress = pl_badge
                        t.status = status_text
                        self._update_single_row(tid)
                elif event_type == "task_completed":
                    tid, final_title, num_files = payload
                    if tid in self.tasks:
                        t = self.tasks[tid]
                        if final_title:
                            t.title = final_title
                        t.progress_pct = 100.0
                        t.speed_str = "—"
                        t.eta_str = "Done"
                        t.status = "Completed"
                        self._append_log(
                            f"[Completed] {t.title} ({num_files} file{'s' if num_files != 1 else ''} saved)"
                        )
                        self._update_single_row(tid)
                        self._refresh_tree()
                elif event_type == "task_partial":
                    tid, final_title, ok_count, err_count, last_err = payload
                    if tid in self.tasks:
                        t = self.tasks[tid]
                        if final_title:
                            t.title = final_title
                        t.progress_pct = 100.0
                        t.speed_str = "—"
                        t.eta_str = "Partial"
                        t.status = f"Partial ({ok_count} ok, {err_count} err)"
                        t.error_message = last_err
                        self._append_log(
                            f"[Partial Playlist] {t.title}: {ok_count} saved, {err_count} failed. Last error: {last_err}"
                        )
                        self._update_single_row(tid)
                        self._refresh_tree()
                elif event_type == "task_cancelled":
                    tid = payload
                    if tid in self.tasks:
                        t = self.tasks[tid]
                        t.status = "Cancelled"
                        t.speed_str = "—"
                        t.eta_str = "—"
                        self._update_single_row(tid)
                        self._refresh_tree()
                elif event_type == "task_failed":
                    tid, err = payload
                    if tid in self.tasks:
                        t = self.tasks[tid]
                        t.progress_pct = 0.0
                        t.status = "Failed (0 saved)"
                        t.speed_str = "—"
                        t.eta_str = "Error"
                        t.error_message = err
                        self._append_log(f"[FAILED] {t.url} -> {err}")
                        if "requested format is not available" in err.lower():
                            self._append_log(
                                "[Tip] Click '↻ Update yt-dlp & FFmpeg Engine' at the top-right to update yt-dlp's YouTube extractor!"
                            )
                        self._update_single_row(tid)
                        self._refresh_tree()
                elif event_type == "batch_finished":
                    self.is_downloading = False
                    self.start_btn.state(["!disabled"])
                    self.cancel_btn.state(["disabled"])
                    done = sum(
                        1 for tid in self.task_order if self.tasks[tid].status == "Completed"
                    )
                    partial = sum(
                        1
                        for tid in self.task_order
                        if self.tasks[tid].status.startswith("Partial")
                    )
                    failed = sum(
                        1
                        for tid in self.task_order
                        if self.tasks[tid].status.startswith("Failed")
                    )
                    self.status_var.set(
                        f"Batch finished: {done} completed, {partial} partial, {failed} failed. "
                        f"Folder: {self.output_dir_var.get()}"
                    )
        except queue.Empty:
            pass

        self.root.after(60, self._poll_ui_queue)


def create_root_window() -> Any:
    """Create a Drag-and-Drop enabled Tkinter root window if tkinterdnd2 is installed."""
    if HAS_DND and TkinterDnD is not None:
        return TkinterDnD.Tk()
    return tk.Tk()


def main() -> int:
    if not HAS_TK:
        sys.stderr.write(
            "Error: 'tkinter' is not installed in this Python environment.\n"
            "  - Windows / macOS: Re-run the Python installer and check 'tcl/tk and IDLE'.\n"
            "  - Ubuntu / Debian: sudo apt install python3-tk\n"
            "  - Fedora / RHEL:   sudo dnf install python3-tkinter\n"
        )
        return 1

    root = create_root_window()
    YouTubeBatchDownloaderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
