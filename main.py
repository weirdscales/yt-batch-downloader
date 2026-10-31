#!/usr/bin/env python3
"""
Android & Desktop Batch YouTube Video/Playlist Downloader (Kivy `main.py`)
==========================================================================
Features in v1.2.0:
1. Dedicated Two-Section Mode & Quality Selector:
   - Toggle between `[ VIDEO MODE ]` and `[ AUDIO ONLY MODE ]`.
   - Video Mode Qualities:
     * 1080p Full HD (MP4 H.264)
     * 720p HD (MP4 H.264)
     * 480p SD (MP4 H.264)
     * 360p Fast / Data Saver (MP4 H.264)
   - Audio Mode Formats & Qualities:
     * M4A (Best Native AAC Audio)
     * MP3 (320kbps High Quality)
     * MP3 (128kbps Standard)
     * WAV (Lossless / Uncompressed Audio)
   - Changing the mode or quality automatically updates all Queued items so
     tapping "Start Batch Download" always honors your current selection.
2. True Audio-Only Downloads & Native Android Hardware Muxing (`MediaMuxer`):
   - In Audio Mode, strictly requests audio-only streams (`140/ba[ext=m4a]/ba[acodec^=mp4a]/ba`)
     so it never downloads a video file. If a fallback stream contains video,
     Android's built-in `MediaExtractor` + `MediaMuxer` strips the video track
     completely.
   - In Video Mode (1080p / 720p / 480p), uses Android's built-in hardware
     `MediaExtractor` + `MediaMuxer` (via `pyjnius`) to losslessly mux separate
     DASH H.264 video (`.mp4`) and AAC audio (`.m4a`) streams into a single `.mp4`
     when `ffmpeg` is not installed on the phone!
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# ---------------------------------------------------------------------------
# 1. SSL Certificate Setup for Android (python-for-android)
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
    try:
        ota_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        ota_dir = Path(tempfile.gettempdir()) / ".ytdlp_ota_pkg"
        ota_dir.mkdir(parents=True, exist_ok=True)
    return ota_dir


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
# 2. Mode & Quality Presets
# ---------------------------------------------------------------------------
MODE_VIDEO = "Video"
MODE_AUDIO = "Audio"

VIDEO_QUALITY_PRESETS: List[str] = [
    "1080p Full HD (MP4)",
    "720p HD (MP4)",
    "480p SD (MP4)",
    "360p Fast / Data Saver (MP4)",
]

AUDIO_QUALITY_PRESETS: List[str] = [
    "M4A (Best AAC Audio)",
    "MP3 (320kbps High Quality)",
    "MP3 (128kbps Standard)",
    "WAV (Uncompressed Audio)",
]

# Ordered client fallback profiles for Android (excludes broken `android_vr` and `ios`)
ANDROID_CLIENT_PROFILES: List[Tuple[str, List[str]]] = [
    ("android", ["android"]),
    ("web_embedded", ["web_embedded"]),
    ("android + web_embedded", ["android", "web_embedded"]),
    ("tv + mweb", ["tv", "mweb"]),
    ("default (no android_vr)", ["default", "-android_vr", "-ios"]),
]

URL_REGEX = re.compile(r"https?://[^\s,;\"'<>]+", re.IGNORECASE)
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
UNSAFE_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class DownloadCancelledError(Exception):
    """Raised inside a yt-dlp progress hook when the user cancels a download."""


def clean_ansi(text: str) -> str:
    """Strip ANSI escape sequences from yt-dlp messages."""
    return ANSI_ESCAPE_RE.sub("", text or "").strip()


def escape_kivy_markup(text: str) -> str:
    """Escape `[`, `]`, and `&` so Kivy `Label(markup=True)` never swallows bracketed text."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("[", "&bl;")
        .replace("]", "&br;")
    )


def sanitize_filename_component(name: str, max_len: int = 80) -> str:
    """Sanitize a folder or file name for Android FAT32 / FUSE storage."""
    cleaned = UNSAFE_PATH_CHARS_RE.sub("_", str(name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        return "YouTube_Media"
    return cleaned[:max_len].strip(" ._")


def is_android() -> bool:
    """Return True when running inside an Android APK (python-for-android)."""
    return "ANDROID_ARGUMENT" in os.environ or "ANDROID_PRIVATE" in os.environ


def get_staging_download_dir() -> Path:
    """Return an internal app-private staging directory where yt-dlp can write freely."""
    base = os.environ.get("ANDROID_PRIVATE")
    if base:
        d = Path(base) / "yt_staging"
    else:
        d = Path(tempfile.gettempdir()) / "yt_batch_staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_default_download_dir() -> Path:
    """Resolve `/storage/emulated/0/Download/YouTube_Downloads` on Android or `~/Downloads/YouTube_Downloads` on PC."""
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


# ---------------------------------------------------------------------------
# 3. Native Android Hardware Muxer & Audio Extractor (`MediaExtractor` + `MediaMuxer`)
# ---------------------------------------------------------------------------
def mux_streams_android(
    video_path: Optional[Path],
    audio_path: Path,
    output_path: Path,
) -> bool:
    """
    Use Android's built-in hardware `MediaExtractor` and `MediaMuxer` (via `pyjnius`)
    to either:
    - Losslessly mux a separate H.264 `.mp4` video stream and AAC `.m4a` audio stream
      into a single `.mp4` file (when `video_path` is provided), OR
    - Strip the video track from a combined `.mp4` file and output a pure `.m4a`
      audio-only file (when `video_path` is None)!
    """
    if not is_android():
        return False
    try:
        from jnius import autoclass  # type: ignore

        MediaExtractor = autoclass("android.media.MediaExtractor")
        MediaMuxer = autoclass("android.media.MediaMuxer")
        MediaFormat = autoclass("android.media.MediaFormat")
        BufferInfo = autoclass("android.media.MediaCodec$BufferInfo")
        ByteBuffer = autoclass("java.nio.ByteBuffer")

        # MUXER_OUTPUT_MPEG_4 = 0
        muxer = MediaMuxer(str(output_path), 0)
        buf = ByteBuffer.allocateDirect(1024 * 1024)
        info = BufferInfo()

        vid_extractor = None
        aud_extractor = None
        mux_vid_track = -1
        mux_aud_track = -1

        try:
            if video_path is not None and video_path.is_file():
                vid_extractor = MediaExtractor()
                vid_extractor.setDataSource(str(video_path))
                for i in range(vid_extractor.getTrackCount()):
                    fmt = vid_extractor.getTrackFormat(i)
                    mime = str(fmt.getString(MediaFormat.KEY_MIME) or "")
                    if mime.startswith("video/"):
                        vid_extractor.selectTrack(i)
                        mux_vid_track = muxer.addTrack(fmt)
                        break

            aud_extractor = MediaExtractor()
            aud_extractor.setDataSource(str(audio_path))
            for i in range(aud_extractor.getTrackCount()):
                fmt = aud_extractor.getTrackFormat(i)
                mime = str(fmt.getString(MediaFormat.KEY_MIME) or "")
                if mime.startswith("audio/"):
                    aud_extractor.selectTrack(i)
                    mux_aud_track = muxer.addTrack(fmt)
                    break

            if mux_aud_track < 0 and mux_vid_track < 0:
                return False

            muxer.start()

            # Copy video samples if muxing video + audio
            if vid_extractor is not None and mux_vid_track >= 0:
                while True:
                    buf.clear()
                    sample_size = vid_extractor.readSampleData(buf, 0)
                    if sample_size < 0:
                        break
                    pts = vid_extractor.getSampleTime()
                    flags = vid_extractor.getSampleFlags()
                    info.set(0, sample_size, pts, flags)
                    muxer.writeSampleData(mux_vid_track, buf, info)
                    vid_extractor.advance()

            # Copy audio samples
            if aud_extractor is not None and mux_aud_track >= 0:
                while True:
                    buf.clear()
                    sample_size = aud_extractor.readSampleData(buf, 0)
                    if sample_size < 0:
                        break
                    pts = aud_extractor.getSampleTime()
                    flags = aud_extractor.getSampleFlags()
                    info.set(0, sample_size, pts, flags)
                    muxer.writeSampleData(mux_aud_track, buf, info)
                    aud_extractor.advance()

            muxer.stop()
            return output_path.is_file() and output_path.stat().st_size > 0
        finally:
            if vid_extractor is not None:
                try:
                    vid_extractor.release()
                except Exception:
                    pass
            if aud_extractor is not None:
                try:
                    aud_extractor.release()
                except Exception:
                    pass
            try:
                muxer.release()
            except Exception:
                pass
    except Exception:
        return False


def get_desired_audio_ext(quality_label: str) -> str:
    """Return the target audio file extension (`.m4a`, `.mp3`, or `.wav`) for Audio Mode."""
    q_upper = (quality_label or "").upper()
    if "MP3" in q_upper:
        return ".mp3"
    if "WAV" in q_upper:
        return ".wav"
    return ".m4a"


def get_desired_video_height(quality_label: str) -> int:
    """Return the maximum target video height (1080, 720, 480, or 360) for Video Mode."""
    if "360p" in quality_label:
        return 360
    if "480p" in quality_label:
        return 480
    if "720p" in quality_label:
        return 720
    return 1080


def export_file_to_public_downloads(
    staged_file: Path,
    public_dir: Path,
    subfolder: str = "",
    forced_ext: Optional[str] = None,
) -> Path:
    """
    Export a completed download from the app's private staging folder into the
    public Downloads directory (`/storage/emulated/0/Download/YouTube_Downloads`).
    Uses `shutil.copyfile` (never `copy2`) to avoid Android FUSE `utime` errors.
    """
    if not staged_file.is_file():
        return staged_file

    ext = forced_ext if forced_ext else staged_file.suffix
    safe_sub = sanitize_filename_component(subfolder) if subfolder else ""
    safe_name = sanitize_filename_component(staged_file.stem, max_len=120) + ext
    target_dir = public_dir / safe_sub if safe_sub else public_dir
    target_path = target_dir / safe_name

    # 1. Try raw byte copy (`shutil.copyfile`) to target subfolder
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(staged_file), str(target_path))
        if target_path.is_file() and target_path.stat().st_size > 0:
            staged_file.unlink(missing_ok=True)
            _notify_android_media_scanner(target_path)
            return target_path
    except Exception:
        pass

    # 2. Try top-level `/storage/emulated/0/Download/<filename>`
    try:
        root_dl = (
            public_dir.parent
            if public_dir.name == "YouTube_Downloads"
            else public_dir
        )
        root_dl.mkdir(parents=True, exist_ok=True)
        fallback_path = root_dl / safe_name
        shutil.copyfile(str(staged_file), str(fallback_path))
        if fallback_path.is_file() and fallback_path.stat().st_size > 0:
            staged_file.unlink(missing_ok=True)
            _notify_android_media_scanner(fallback_path)
            return fallback_path
    except Exception:
        pass

    # 3. Fallback for strict Android 10–14 Scoped Storage via MediaStore API
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
            values.put(MediaColumns.DISPLAY_NAME, safe_name)
            ext_lower = ext.lower()
            if ext_lower == ".mp3":
                mime = "audio/mpeg"
            elif ext_lower == ".wav":
                mime = "audio/wav"
            elif ext_lower in (".m4a", ".aac", ".opus", ".ogg"):
                mime = "audio/mp4"
            else:
                mime = "video/mp4"
            values.put(MediaColumns.MIME_TYPE, mime)
            rel_path = Environment.DIRECTORY_DOWNLOADS + "/YouTube_Downloads"
            if safe_sub:
                rel_path = f"{rel_path}/{safe_sub}"
            values.put(MediaColumns.RELATIVE_PATH, rel_path)

            uri = resolver.insert(MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
            if uri is not None:
                out_stream = resolver.openOutputStream(uri)
                with open(staged_file, "rb") as in_fh:
                    while True:
                        chunk = in_fh.read(65536)
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
    """Notify Android's MediaScanner so the new file appears in Gallery/Music/Files."""
    if not is_android():
        return
    try:
        from jnius import autoclass  # type: ignore

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        MediaScannerConnection = autoclass("android.media.MediaScannerConnection")
        context = PythonActivity.mActivity
        MediaScannerConnection.scanFile(context, [str(file_path)], None, None)
    except Exception:
        pass


def update_ytdlp_ota(log_callback: Optional[Any] = None) -> Tuple[bool, str]:
    """Download the latest `yt-dlp` wheel from PyPI and hot-reload it in memory."""
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
        req_whl = urllib.request.Request(
            wheel_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req_whl, timeout=60) as resp_whl, open(
            whl_path, "wb"
        ) as out_f:
            shutil.copyfileobj(resp_whl, out_f)

        old_pkg = ota_dir / "yt_dlp"
        if old_pkg.exists():
            shutil.rmtree(old_pkg, ignore_errors=True)

        with zipfile.ZipFile(whl_path, "r") as zf:
            zf.extractall(str(ota_dir))
        whl_path.unlink(missing_ok=True)

        if str(ota_dir) not in sys.path:
            sys.path.insert(0, str(ota_dir))

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
    """State for a single queued video or audio item."""

    task_id: str
    url: str
    is_playlist: bool
    media_mode: str  # "Video" or "Audio"
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
    """Strip tracking parameters (`?si=...`, `&pp=...`) and normalize `youtube.com` URLs."""
    try:
        parsed = urlparse(url.strip())
        query = parse_qs(parsed.query, keep_blank_values=False)
        for tracking_key in ("si", "pp", "feature", "ab_channel"):
            query.pop(tracking_key, None)
        clean_query = urlencode(query, doseq=True)
        netloc = parsed.netloc
        if netloc.lower() in ("youtube.com", "m.youtube.com"):
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


def get_playlist_id_if_expandable(url: str, expand_playlists: bool) -> Optional[str]:
    """Return the YouTube playlist ID if `expand_playlists` is enabled and the URL is a playlist."""
    if not expand_playlists:
        return None
    try:
        parsed = urlparse(url.strip())
        query = parse_qs(parsed.query)
        list_vals = query.get("list")
        if not list_vals:
            return None
        pl_id = list_vals[0].strip()
        if not pl_id:
            return None
        if "/playlist" in parsed.path.lower():
            return pl_id
        if not pl_id.startswith("RD"):
            return pl_id
    except Exception:
        pass
    return None


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
    """Fetch `(video_url, title)` entries inside a playlist using `extract_flat='in_playlist'`."""
    if not HAS_YTDLP:
        return "", []

    pl_id = get_playlist_id_if_expandable(url, expand_playlists=True)
    target_url = f"https://www.youtube.com/playlist?list={pl_id}" if pl_id else url

    opts: Dict[str, Any] = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"player_client": ["web", "android", "mweb"]}},
    }
    entries_out: List[Tuple[str, str]] = []
    playlist_title = "YouTube_Playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target_url, download=False)
        if isinstance(info, dict):
            playlist_title = sanitize_filename_component(
                str(info.get("title") or "YouTube_Playlist")
            )
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
                if vid_url and vid_title not in (
                    "[Private video]",
                    "[Deleted video]",
                ):
                    entries_out.append((vid_url, vid_title))
    return playlist_title, entries_out


def build_mobile_ytdlp_options(
    output_dir: Path,
    quality_label: str,
    media_mode: str = MODE_VIDEO,
    expand_playlists: bool = False,
    progress_hook: Optional[Any] = None,
    logger: Optional[Any] = None,
    player_clients: Optional[List[str]] = None,
    custom_outtmpl: Optional[str] = None,
    custom_format: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build `yt-dlp` options that strictly distinguish between `MODE_VIDEO` and `MODE_AUDIO`:
    - When `media_mode == "Audio"` (or `"Audio"` / `"M4A"` / `"MP3"` / `"WAV"` in `quality_label`),
      STRICTLY selects audio-only formats (`140/ba[ext=m4a]/ba[acodec^=mp4a]/ba`) and NEVER
      appends `/b` (video) so audio mode never downloads a video!
    - When `media_mode == "Video"`, respects the exact resolution (`1080p`, `720p`, `480p`, `360p`)
      and H.264 (`avc1`) + AAC (`mp4a`) codec preferences.
    """
    output_dir = Path(output_dir).expanduser().resolve()
    ffmpeg_path = shutil.which("ffmpeg")
    has_sys_ffmpeg = ffmpeg_path is not None and not is_android()

    outtmpl = custom_outtmpl or str(
        output_dir / "%(title).110B [%(id)s].%(ext)s"
    )

    if player_clients is None:
        player_clients = ["android", "web_embedded"]

    opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": not expand_playlists,
        "ignoreerrors": False,
        "continuedl": True,
        "nopart": False,
        "updatetime": False,
        "http_chunk_size": 10 * 1024 * 1024,
        "retries": 6,
        "fragment_retries": 6,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "concurrent_fragment_downloads": 1,
        "windowsfilenames": True,
        "restrictfilenames": False,
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

    if custom_format is not None:
        opts["format"] = custom_format
        return opts

    is_audio_request = (
        media_mode == MODE_AUDIO
        or any(
            tok in (quality_label or "").upper()
            for tok in ("AUDIO", "M4A", "MP3", "WAV")
        )
    )

    if is_audio_request:
        # STRICT AUDIO-ONLY FORMAT: Never include `/b` (combined video) here!
        opts["format"] = "140/ba[ext=m4a]/ba[acodec^=mp4a]/ba"
        if "128kbps" in quality_label:
            opts["format_sort"] = ["abr:128", "acodec:aac", "aext:m4a:mp3"]
        else:
            opts["format_sort"] = ["abr:320", "acodec:aac", "aext:m4a:mp3"]

        if has_sys_ffmpeg:
            target_ext = get_desired_audio_ext(quality_label).lstrip(".")
            pref_q = "320" if "320" in quality_label else "192"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": target_ext,
                    "preferredquality": pref_q,
                }
            ]
        return opts

    # VIDEO MODE: Respect exact resolution (1080p, 720p, 480p, 360p) + H.264
    max_h = get_desired_video_height(quality_label)

    if has_sys_ffmpeg:
        opts["format"] = (
            f"bv*[height<={max_h}][vcodec^=avc]+ba[acodec^=mp4a]/"
            f"bv*[height<={max_h}][vcodec^=avc]+ba/"
            f"b[height<={max_h}][ext=mp4][vcodec!*=av01]/"
            f"bv*[height<={max_h}][vcodec!*=av01]+ba/"
            "18/b[ext=mp4]"
        )
        opts["merge_output_format"] = "mp4"
    else:
        # On Android without FFmpeg: match pre-merged MP4 up to max_h first (never hardcode 18 first!)
        opts["format"] = (
            f"b[height<={max_h}][ext=mp4][vcodec^=avc]/"
            f"b[height<={max_h}][ext=mp4][vcodec!*=av01]/"
            f"b[height<={max_h}][ext=mp4]/"
            "18/b[ext=mp4][vcodec!*=av01]/b"
        )

    opts["format_sort"] = [
        f"res:{max_h}",
        "vcodec:h264",
        "acodec:aac",
        "ext:mp4:m4a",
    ]
    return opts


def collect_downloaded_files(
    staging_dir: Path,
    info: Optional[Dict[str, Any]],
    hook_files: List[Path],
    ydl: Any,
) -> List[Path]:
    """Collect all completed media files from a yt-dlp run."""
    found: List[Path] = []

    def _add_if_valid(p: Path) -> None:
        try:
            if (
                p.is_file()
                and p.stat().st_size > 0
                and not p.name.endswith((".part", ".ytdl", ".temp"))
                and p not in found
            ):
                found.append(p)
        except Exception:
            pass

    for hf in hook_files:
        _add_if_valid(hf)

    if isinstance(info, dict):
        for req_dl in info.get("requested_downloads") or []:
            if isinstance(req_dl, dict):
                fp_str = req_dl.get("filepath") or req_dl.get("_filename")
                if fp_str:
                    _add_if_valid(Path(fp_str))
        try:
            if ydl is not None:
                prep = ydl.prepare_filename(info)
                if prep:
                    _add_if_valid(Path(prep))
        except Exception:
            pass

        vid_id = info.get("id")
        if vid_id and staging_dir.is_dir():
            for cand in staging_dir.iterdir():
                if f"[{vid_id}]" in cand.name:
                    _add_if_valid(cand)

    return found


# ---------------------------------------------------------------------------
# 4. Kivy Mobile UI
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
            self.title = "YT Batch Downloader (Video & Audio)"
            request_android_permissions()

            self.state_lock = threading.Lock()
            self.tasks: Dict[str, MobileDownloadTask] = {}
            self.task_order: List[str] = []
            self._counter = 0
            self.current_mode = MODE_VIDEO
            self.ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
            self.cancel_event = threading.Event()
            self.is_downloading = False
            self.did_auto_ota = False
            self.output_dir = get_default_download_dir()
            self.staging_dir = get_staging_download_dir()

            root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(7))

            # 1. Header + OTA Updater Button
            top_header = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
            ver_str = (
                getattr(yt_dlp.version, "__version__", "unknown")
                if HAS_YTDLP
                else "missing"
            )
            self.header_lbl = Label(
                text=(
                    f"[b]YouTube Batch & Playlist Downloader[/b]\n"
                    f"[size=11sp]yt-dlp {escape_kivy_markup(ver_str)}  •  Video (MP4) & Audio (M4A/MP3/WAV)[/size]"
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
            btn_bar = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(6))
            paste_btn = Button(
                text="Paste",
                background_normal="",
                background_color=(0.15, 0.39, 0.92, 1),
                color=(1, 1, 1, 1),
                font_size=sp(12),
            )
            paste_btn.bind(on_release=self._on_paste)

            clear_input_btn = Button(
                text="Clear Box",
                background_normal="",
                background_color=(0.40, 0.45, 0.55, 1),
                color=(1, 1, 1, 1),
                font_size=sp(12),
            )
            clear_input_btn.bind(
                on_release=lambda *_: setattr(self.url_input, "text", "")
            )

            clear_queue_btn = Button(
                text="Clear Queue",
                background_normal="",
                background_color=(0.55, 0.35, 0.15, 1),
                color=(1, 1, 1, 1),
                font_size=sp(12),
            )
            clear_queue_btn.bind(on_release=self._on_clear_queue)

            add_btn = Button(
                text="+ Add Queue",
                background_normal="",
                background_color=(0.08, 0.50, 0.24, 1),
                color=(1, 1, 1, 1),
                bold=True,
                font_size=sp(12),
            )
            add_btn.bind(on_release=self._on_add_urls)

            btn_bar.add_widget(paste_btn)
            btn_bar.add_widget(clear_input_btn)
            btn_bar.add_widget(clear_queue_btn)
            btn_bar.add_widget(add_btn)
            root.add_widget(btn_bar)

            self.url_input = TextInput(
                hint_text="Paste 10+ YouTube video links or a Playlist URL here...",
                multiline=True,
                size_hint_y=None,
                height=dp(74),
                font_size=sp(13),
                background_color=(1, 1, 1, 1),
                foreground_color=(0.06, 0.09, 0.16, 1),
            )
            root.add_widget(self.url_input)

            # 3. Dedicated Two-Row Mode (VIDEO vs AUDIO) + Quality Selector
            mode_bar = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(8))
            self.video_mode_btn = Button(
                text="VIDEO (MP4)",
                background_normal="",
                background_color=(0.86, 0.15, 0.15, 1),
                color=(1, 1, 1, 1),
                bold=True,
                font_size=sp(13),
            )
            self.video_mode_btn.bind(
                on_release=lambda *_: self._switch_mode(MODE_VIDEO)
            )

            self.audio_mode_btn = Button(
                text="AUDIO ONLY (M4A / MP3 / WAV)",
                background_normal="",
                background_color=(0.64, 0.69, 0.76, 1),
                color=(0.12, 0.16, 0.23, 1),
                bold=True,
                font_size=sp(13),
            )
            self.audio_mode_btn.bind(
                on_release=lambda *_: self._switch_mode(MODE_AUDIO)
            )

            mode_bar.add_widget(self.video_mode_btn)
            mode_bar.add_widget(self.audio_mode_btn)
            root.add_widget(mode_bar)

            opts_bar = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(8))
            self.quality_spinner = Spinner(
                text=VIDEO_QUALITY_PRESETS[0],
                values=VIDEO_QUALITY_PRESETS,
                size_hint_x=0.62,
                font_size=sp(12),
                background_normal="",
                background_color=(0.15, 0.23, 0.36, 1),
                color=(1, 1, 1, 1),
            )
            self.quality_spinner.bind(text=self._on_quality_changed)
            opts_bar.add_widget(self.quality_spinner)

            pl_box = BoxLayout(size_hint_x=0.38, spacing=dp(4))
            self.expand_pl_check = CheckBox(
                active=True, size_hint_x=None, width=dp(28)
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
                height=dp(18),
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
                text="[Ready] Choose VIDEO or AUDIO mode above, paste links, and tap 'Start Batch Download'.\n",
                readonly=True,
                multiline=True,
                size_hint_y=None,
                height=dp(90),
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

        def _switch_mode(self, new_mode: str) -> None:
            self.current_mode = new_mode
            if new_mode == MODE_VIDEO:
                self.video_mode_btn.background_color = (0.86, 0.15, 0.15, 1)
                self.video_mode_btn.color = (1, 1, 1, 1)
                self.audio_mode_btn.background_color = (0.64, 0.69, 0.76, 1)
                self.audio_mode_btn.color = (0.12, 0.16, 0.23, 1)
                self.quality_spinner.values = VIDEO_QUALITY_PRESETS
                self.quality_spinner.text = VIDEO_QUALITY_PRESETS[0]
            else:
                self.audio_mode_btn.background_color = (0.08, 0.50, 0.24, 1)
                self.audio_mode_btn.color = (1, 1, 1, 1)
                self.video_mode_btn.background_color = (0.64, 0.69, 0.76, 1)
                self.video_mode_btn.color = (0.12, 0.16, 0.23, 1)
                self.quality_spinner.values = AUDIO_QUALITY_PRESETS
                self.quality_spinner.text = AUDIO_QUALITY_PRESETS[0]

            self._sync_pending_tasks_format()
            self._append_log(
                f"[Mode] Switched to {new_mode.upper()} -> {self.quality_spinner.text}"
            )

        def _on_quality_changed(self, _spinner: Any, new_text: str) -> None:
            self._sync_pending_tasks_format()

        def _sync_pending_tasks_format(self) -> None:
            """Update all Queued/Failed/Cancelled tasks to match the currently selected mode & quality."""
            with self.state_lock:
                for tid in self.task_order:
                    t = self.tasks.get(tid)
                    if t and t.status in ("Queued", "Failed", "Cancelled"):
                        t.media_mode = self.current_mode
                        t.quality_label = self.quality_spinner.text
            self._rebuild_queue_cards()

        def _append_log(self, text: str) -> None:
            self.log_box.text += f"{text.strip()}\n"
            self.log_box.cursor = (0, len(self.log_box.text.splitlines()))

        def _register_task_sync(
            self,
            url: str,
            is_pl: bool,
            mode: str,
            qual: str,
            title: str,
            pl_folder: str,
            badge: str,
        ) -> str:
            with self.state_lock:
                self._counter += 1
                tid = f"item_{self._counter}"
                self.tasks[tid] = MobileDownloadTask(
                    task_id=tid,
                    url=url,
                    is_playlist=is_pl,
                    media_mode=mode,
                    quality_label=qual,
                    title=title,
                    playlist_folder=pl_folder,
                    playlist_badge=badge,
                )
                self.task_order.append(tid)
                return tid

        def _expand_urls_into_tasks_sync(
            self,
            urls: List[str],
            expand_pl: bool,
            media_mode: str,
            quality_label: str,
        ) -> int:
            added = 0
            for url in urls:
                if self.cancel_event.is_set():
                    break
                pl_id = get_playlist_id_if_expandable(url, expand_playlists=expand_pl)
                if pl_id:
                    self.ui_queue.put(
                        ("log", "[Playlist] Fetching video list from playlist...")
                    )
                    try:
                        pl_title, entries = expand_playlist_flat(url)
                        if entries:
                            total = len(entries)
                            for idx, (v_url, v_title) in enumerate(entries, start=1):
                                self._register_task_sync(
                                    v_url,
                                    False,
                                    media_mode,
                                    quality_label,
                                    v_title,
                                    pl_title,
                                    f"[{idx}/{total}]",
                                )
                                added += 1
                            self.ui_queue.put(("refresh_ui", None))
                            self.ui_queue.put(
                                (
                                    "log",
                                    f"[Playlist] Expanded '{pl_title}' into {total} items ({media_mode}: {quality_label}).",
                                )
                            )
                            continue
                    except Exception as exc:
                        self.ui_queue.put(
                            (
                                "log",
                                f"[Playlist Notice] Flat expand fallback ({exc}); queueing URL directly.",
                            )
                        )

                self._register_task_sync(
                    url, False, media_mode, quality_label, url, "", ""
                )
                added += 1
                self.ui_queue.put(("refresh_ui", None))
            return added

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

        def _on_clear_queue(self, *_: Any) -> None:
            if self.is_downloading:
                self._append_log("[Notice] Stop active downloads before clearing queue.")
                return
            with self.state_lock:
                self.tasks.clear()
                self.task_order.clear()
            self.progress_bar.value = 0
            self._rebuild_queue_cards()
            self._append_log("[Queue] Cleared all items.")

        def _on_add_urls(self, *_: Any) -> None:
            urls = extract_urls_from_text(self.url_input.text)
            if not urls:
                self._append_log(
                    "[Notice] Paste at least one valid https:// YouTube link first."
                )
                return

            expand_pl = bool(self.expand_pl_check.active)
            media_mode = self.current_mode
            quality_label = self.quality_spinner.text
            self.url_input.text = ""

            def _bg_add() -> None:
                added = self._expand_urls_into_tasks_sync(
                    urls, expand_pl, media_mode, quality_label
                )
                self.ui_queue.put(
                    (
                        "log",
                        f"[Queue] Added {added} item(s) as {media_mode.upper()} ({quality_label}).",
                    )
                )

            threading.Thread(target=_bg_add, daemon=True).start()

        def _rebuild_queue_cards(self) -> None:
            self.queue_container.clear_widgets()
            with self.state_lock:
                order_snapshot = list(self.task_order)

            for idx, tid in enumerate(order_snapshot, start=1):
                t = self.tasks.get(tid)
                if not t:
                    continue
                badge_escaped = (
                    f" {escape_kivy_markup(t.playlist_badge)}"
                    if t.playlist_badge
                    else ""
                )
                mode_tag = escape_kivy_markup(f"[{t.media_mode.upper()} • {t.quality_label}]")
                title_escaped = escape_kivy_markup(t.title)
                status_escaped = escape_kivy_markup(t.status)
                row = Label(
                    text=(
                        f"[b]#{idx}{badge_escaped} {mode_tag}[/b] {title_escaped}\n"
                        f"Status: {status_escaped}  |  {t.progress_pct:.1f}%  |  Speed: {t.speed_str}  |  ETA: {t.eta_str}"
                    ),
                    markup=True,
                    size_hint_y=None,
                    height=dp(50),
                    font_size=sp(12),
                    color=(0.06, 0.09, 0.16, 1),
                    halign="left",
                    valign="middle",
                )
                row.bind(size=lambda inst, val: setattr(inst, "text_size", val))
                self.queue_container.add_widget(row)

            if order_snapshot:
                avg = sum(
                    self.tasks[k].progress_pct
                    for k in order_snapshot
                    if k in self.tasks
                ) / len(order_snapshot)
                self.progress_bar.value = avg

        def _on_start_batch(self, *_: Any) -> None:
            if self.is_downloading:
                return
            raw_box = self.url_input.text.strip()
            urls = extract_urls_from_text(raw_box) if raw_box else []
            if raw_box:
                self.url_input.text = ""
            expand_pl = bool(self.expand_pl_check.active)
            media_mode = self.current_mode
            quality_label = self.quality_spinner.text
            self._start_with_urls_and_queue(
                urls, expand_pl, media_mode, quality_label
            )

        def _start_with_urls_and_queue(
            self,
            new_urls: List[str],
            expand_pl: bool,
            media_mode: str,
            quality_label: str,
        ) -> None:
            self.cancel_event.clear()
            self.is_downloading = True
            self.start_btn.disabled = True
            self.stop_btn.disabled = False

            def coordinator() -> None:
                if new_urls:
                    self._expand_urls_into_tasks_sync(
                        new_urls, expand_pl, media_mode, quality_label
                    )

                with self.state_lock:
                    queued = [
                        tid
                        for tid in self.task_order
                        if self.tasks[tid].status in ("Queued", "Failed", "Cancelled")
                    ]
                    for tid in queued:
                        self.tasks[tid].status = "Queued"
                        self.tasks[tid].progress_pct = 0.0
                        # Always sync queued items to the user's active Mode & Quality
                        self.tasks[tid].media_mode = media_mode
                        self.tasks[tid].quality_label = quality_label

                self.ui_queue.put(("refresh_ui", None))

                if not queued:
                    self.ui_queue.put(("log", "[Notice] No queued items to download."))
                    self.ui_queue.put(("batch_done", None))
                    return

                self.ui_queue.put(
                    (
                        "log",
                        f"[Batch Start] Downloading {len(queued)} item(s) as {media_mode.upper()} ({quality_label}) -> {self.output_dir}",
                    )
                )

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self._worker, tid) for tid in queued]
                    concurrent.futures.wait(futures)

                self.ui_queue.put(("batch_done", None))

            threading.Thread(target=coordinator, daemon=True).start()

        def _on_stop_batch(self, *_: Any) -> None:
            if self.is_downloading:
                self.cancel_event.set()
                self._append_log("[User Action] Stopping active downloads...")

        def _download_dash_hd_and_mux_android(
            self,
            tid: str,
            task: MobileDownloadTask,
            max_h: int,
            hook: Any,
            clients: List[str],
        ) -> Optional[Path]:
            """
            When running on Android (no FFmpeg) and the user selected 1080p, 720p, or 480p,
            attempt to download the separate H.264 video stream (`bv*`) and AAC audio stream
            (`140`) and losslessly mux them into a single `.mp4` using Android's `MediaMuxer`!
            """
            if not is_android() or max_h <= 360:
                return None

            vid_tmpl = str(self.staging_dir / f"vid_{tid}_%(id)s.%(ext)s")
            aud_tmpl = str(self.staging_dir / f"aud_{tid}_%(id)s.%(ext)s")
            vid_hooks: List[Path] = []
            aud_hooks: List[Path] = []

            def v_hook(d: Dict[str, Any]) -> None:
                hook(d)
                if d.get("status") == "finished" and d.get("filename"):
                    vid_hooks.append(Path(d["filename"]))

            def a_hook(d: Dict[str, Any]) -> None:
                if self.cancel_event.is_set():
                    raise DownloadCancelledError("Cancelled")
                if d.get("status") == "finished" and d.get("filename"):
                    aud_hooks.append(Path(d["filename"]))

            v_opts = build_mobile_ytdlp_options(
                output_dir=self.staging_dir,
                quality_label=task.quality_label,
                media_mode=MODE_VIDEO,
                progress_hook=v_hook,
                player_clients=clients,
                custom_outtmpl=vid_tmpl,
                custom_format=f"bv*[height<={max_h}][vcodec^=avc][ext=mp4]/bv*[height<={max_h}][ext=mp4]",
            )
            a_opts = build_mobile_ytdlp_options(
                output_dir=self.staging_dir,
                quality_label="M4A (Best AAC Audio)",
                media_mode=MODE_AUDIO,
                progress_hook=a_hook,
                player_clients=clients,
                custom_outtmpl=aud_tmpl,
                custom_format="140/ba[ext=m4a]/ba[acodec^=mp4a]",
            )

            vid_fp: Optional[Path] = None
            aud_fp: Optional[Path] = None
            try:
                with yt_dlp.YoutubeDL(v_opts) as ydl_v:
                    info_v = ydl_v.extract_info(task.url, download=True)
                    if isinstance(info_v, dict):
                        task.title = info_v.get("title") or task.title
                    v_list = collect_downloaded_files(
                        self.staging_dir, info_v, vid_hooks, ydl_v
                    )
                    if v_list:
                        vid_fp = v_list[-1]

                if not vid_fp or not vid_fp.is_file():
                    return None

                with yt_dlp.YoutubeDL(a_opts) as ydl_a:
                    info_a = ydl_a.extract_info(task.url, download=True)
                    a_list = collect_downloaded_files(
                        self.staging_dir, info_a, aud_hooks, ydl_a
                    )
                    if a_list:
                        aud_fp = a_list[-1]

                if not aud_fp or not aud_fp.is_file():
                    return None

                self.ui_queue.put(("status", (tid, f"Muxing {max_h}p MP4...", 99.0)))
                safe_title = sanitize_filename_component(task.title, max_len=100)
                vid_id = info_v.get("id", tid) if isinstance(info_v, dict) else tid
                muxed_out = self.staging_dir / f"{safe_title} [{vid_id}].mp4"
                if mux_streams_android(vid_fp, aud_fp, muxed_out):
                    return muxed_out
                return None
            except DownloadCancelledError:
                raise
            except Exception:
                return None
            finally:
                if vid_fp and vid_fp.exists():
                    vid_fp.unlink(missing_ok=True)
                if aud_fp and aud_fp.exists():
                    aud_fp.unlink(missing_ok=True)

        def _worker(self, tid: str) -> None:
            task = self.tasks[tid]
            if self.cancel_event.is_set():
                self.ui_queue.put(("status", (tid, "Cancelled", 0.0)))
                return

            is_audio = task.media_mode == MODE_AUDIO
            target_audio_ext = (
                get_desired_audio_ext(task.quality_label) if is_audio else None
            )
            max_h = get_desired_video_height(task.quality_label)

            self.ui_queue.put(("status", (tid, "Connecting...", 0.0)))
            hook_files: List[Path] = []

            def hook(d: Dict[str, Any]) -> None:
                if self.cancel_event.is_set():
                    raise DownloadCancelledError("Cancelled")
                st = d.get("status")
                info_dict = d.get("info_dict") or {}
                title = info_dict.get("title") or task.title

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
                                "Downloading Audio" if is_audio else "Downloading Video",
                            ),
                        )
                    )
                elif st == "finished":
                    fn = d.get("filename") or ""
                    if fn:
                        fp = Path(fn)
                        if fp not in hook_files:
                            hook_files.append(fp)
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
                                "Saving Audio..." if is_audio else "Saving MP4...",
                            ),
                        )
                    )

            last_err = "Download failed (0 files saved)."

            for profile_name, clients in ANDROID_CLIENT_PROFILES:
                if self.cancel_event.is_set():
                    self.ui_queue.put(("status", (tid, "Cancelled", 0.0)))
                    return

                # 1. If on Android in Video Mode (480p / 720p / 1080p) and client supports DASH (`android`),
                # try downloading H.264 video + AAC audio and hardware-muxing via MediaMuxer first!
                if not is_audio and is_android() and max_h > 360 and "android" in clients:
                    try:
                        muxed_fp = self._download_dash_hd_and_mux_android(
                            tid, task, max_h, hook, clients
                        )
                        if muxed_fp and muxed_fp.is_file():
                            out_fp = export_file_to_public_downloads(
                                muxed_fp,
                                self.output_dir,
                                subfolder=task.playlist_folder,
                            )
                            self.ui_queue.put(("status", (tid, "Completed", 100.0)))
                            self.ui_queue.put(
                                ("log", f"[Saved {max_h}p MP4] {task.title} -> {out_fp}")
                            )
                            return
                    except DownloadCancelledError:
                        self.ui_queue.put(("status", (tid, "Cancelled", 0.0)))
                        return
                    except Exception:
                        pass

                # 2. Standard single-stream download (Audio-Only or pre-merged Video)
                for resume_cycle in range(2):
                    hook_files.clear()
                    logger = YtDlpTaskLogger()
                    opts = build_mobile_ytdlp_options(
                        output_dir=self.staging_dir,
                        quality_label=task.quality_label,
                        media_mode=task.media_mode,
                        expand_playlists=False,
                        progress_hook=hook,
                        logger=logger,
                        player_clients=clients,
                    )
                    info_result: Optional[Dict[str, Any]] = None
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            res = ydl.extract_info(task.url, download=True)
                            if isinstance(res, dict):
                                info_result = res
                                task.title = res.get("title") or task.title
                            ready_files = collect_downloaded_files(
                                self.staging_dir, info_result, hook_files, ydl
                            )
                    except DownloadCancelledError:
                        self.ui_queue.put(("status", (tid, "Cancelled", 0.0)))
                        return
                    except Exception as exc:
                        err_s = clean_ansi(str(exc))
                        if err_s and err_s not in logger.errors:
                            logger.errors.append(err_s)
                        ready_files = collect_downloaded_files(
                            self.staging_dir, info_result, hook_files, None
                        )

                    if ready_files:
                        saved_paths: List[Path] = []
                        for staged_fp in ready_files:
                            out_fp = export_file_to_public_downloads(
                                staged_fp,
                                self.output_dir,
                                subfolder=task.playlist_folder,
                                forced_ext=target_audio_ext,
                            )
                            saved_paths.append(out_fp)
                        self.ui_queue.put(("status", (tid, "Completed", 100.0)))
                        self.ui_queue.put(
                            (
                                "log",
                                f"[Saved {task.media_mode}] {task.title} -> {saved_paths[-1]}",
                            )
                        )
                        return

                    if logger.errors:
                        last_err = logger.errors[-1]
                        if "403" in last_err and resume_cycle == 0:
                            time.sleep(0.5)
                            continue
                    break

                # 3. Special fallback for Audio Mode on `web_embedded` (which only serves format 18 MP4):
                # Download format 18 and strip the video track using Android's MediaExtractor + MediaMuxer!
                if is_audio and is_android() and "web_embedded" in clients:
                    try:
                        hook_files.clear()
                        opts_18 = build_mobile_ytdlp_options(
                            output_dir=self.staging_dir,
                            quality_label="360p Fast / Data Saver (MP4)",
                            media_mode=MODE_VIDEO,
                            progress_hook=hook,
                            player_clients=["web_embedded"],
                            custom_format="18/b[ext=mp4]",
                        )
                        with yt_dlp.YoutubeDL(opts_18) as ydl_18:
                            res_18 = ydl_18.extract_info(task.url, download=True)
                            if isinstance(res_18, dict):
                                task.title = res_18.get("title") or task.title
                            mp4_files = collect_downloaded_files(
                                self.staging_dir, res_18, hook_files, ydl_18
                            )
                        if mp4_files:
                            src_mp4 = mp4_files[-1]
                            pure_audio_fp = src_mp4.with_suffix(".m4a")
                            if mux_streams_android(None, src_mp4, pure_audio_fp):
                                src_mp4.unlink(missing_ok=True)
                                out_fp = export_file_to_public_downloads(
                                    pure_audio_fp,
                                    self.output_dir,
                                    subfolder=task.playlist_folder,
                                    forced_ext=target_audio_ext,
                                )
                                self.ui_queue.put(("status", (tid, "Completed", 100.0)))
                                self.ui_queue.put(
                                    ("log", f"[Saved Audio] {task.title} -> {out_fp}")
                                )
                                return
                    except Exception:
                        pass

                self.ui_queue.put(
                    (
                        "log",
                        f"[{tid}] Client '{profile_name}' failed ({last_err}); trying next...",
                    )
                )

            self.ui_queue.put(("status", (tid, "Failed", 0.0)))
            self.ui_queue.put(("log", f"[FAILED] {task.title} -> {last_err}"))

        def _poll_queue(self, _dt: float) -> None:
            dirty = False
            try:
                while True:
                    kind, payload = self.ui_queue.get_nowait()
                    if kind == "log":
                        self._append_log(str(payload))
                    elif kind == "refresh_ui":
                        dirty = True
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
                            f"[size=11sp]yt-dlp {escape_kivy_markup(ver_str)}  •  Video (MP4) & Audio (M4A/MP3/WAV)[/size]"
                        )
                        self._append_log(
                            f"[OTA Updater] {'SUCCESS' if ok else 'ERROR'}: {msg}"
                        )
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
                        with self.state_lock:
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