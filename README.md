<div align="center">

# YT Batch Downloader

### Paste a pile of YouTube links. Pick Video or Audio. Walk away.

Batch and full-playlist downloads for **Android and desktop** — powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp), with no ads, no accounts, and no analytics.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Platforms](https://img.shields.io/badge/platforms-Android%20%7C%20Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![Built with Kivy](https://img.shields.io/badge/built%20with-Kivy-orange)
![Powered by yt-dlp](https://img.shields.io/badge/powered%20by-yt--dlp-red)

</div>

<!--
  Add screenshots here before publishing, e.g.:
  <p align="center">
    <img src="docs/screenshot-queue.png" width="260" alt="Download queue">
    <img src="docs/screenshot-format.png" width="260" alt="Video / Audio selector">
  </p>
-->

---

## Why YT Batch Downloader?

Many downloader apps handle one link at a time and wrap it in ads. This one is built for **batches**: paste ten links or a whole playlist, choose what you want once, and let it run.

- 📋 **True batch queue.** Paste any number of links at once, mixed with other text if you like. The app finds the URLs for you.
- 📃 **Full playlist support.** Tick *Full Playlists* and a playlist expands into individual queue items, saved into a folder named after the playlist.
- 🎛️ **A format picker that does what it says.** Choose **Video** (Best / 1080p / 720p / 480p / 360p) or **Audio** (M4A / MP3 / WAV). Whatever is selected when you press **Start** applies to everything waiting in the queue.
- 🛡️ **Built to survive YouTube's 403 errors.** It rotates through several player-client profiles, downloads in 10 MB chunks, and resumes interrupted downloads instead of starting over.
- 🔄 **Update the engine without reinstalling.** One tap on **Update Engine** pulls the latest yt-dlp from PyPI, even inside an installed APK. If every client hits a 403, the app tries this once on its own.
- 📱 **Android storage done right.** Files land in `Download/YouTube_Downloads`, with fixes for Android 11–14 Scoped Storage (including a MediaStore fallback).
- 🔍 **Open and auditable.** The whole app is one readable Python file. The only network traffic is YouTube (via yt-dlp) and PyPI when updating the engine.

---

## Choose exactly what you get

| You pick | What you get |
|---|---|
| **Video → Best / 1080p / 720p / 480p / 360p** | H.264 + AAC MP4, capped at the height you chose |
| **Audio → M4A** | AAC audio in an `.m4a` file, no extra tools needed |
| **Audio → MP3** | 192 kbps MP3 (needs FFmpeg) |
| **Audio → WAV** | Uncompressed WAV (needs FFmpeg) |

### What needs FFmpeg?

Merging separate HD video and audio streams, and converting to MP3 or WAV, both need [FFmpeg](https://ffmpeg.org/). The app checks for a working copy and **tells you in the log what it did instead** when there isn't one. It never quietly hands you something different.

| | Desktop with FFmpeg | Desktop without FFmpeg | Android APK (today) |
|---|:---:|:---:|:---:|
| Video up to 360p | ✅ | ✅ | ✅ |
| Video 480p – 1080p / Best | ✅ merged MP4 | ⚠️ best single-file stream (usually 360p) | ⚠️ best single-file stream (usually 360p) |
| Audio M4A | ✅ | ✅ | ✅ |
| Audio MP3 / WAV | ✅ | ⚠️ saved as M4A | ⚠️ saved as M4A |

> **Bundling FFmpeg into the Android APK** is the biggest open item on the [roadmap](#roadmap--help-wanted). The app already looks for a binary shipped as `libffmpeg.so` and uses it if it runs. If you can help package one, see [Contributing](#contributing).

---

## Quick start — Desktop

Works on Windows, macOS, and Linux.

```bash
git clone https://github.com/YOUR_USERNAME/yt-batch-downloader.git
cd yt-batch-downloader

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install kivy yt-dlp certifi
python main.py
```

Optional, for HD merging and MP3/WAV: install FFmpeg.

```bash
winget install Gyan.FFmpeg         # Windows
brew install ffmpeg                # macOS
sudo apt install ffmpeg            # Debian / Ubuntu
```

Downloads go to `~/Downloads/YouTube_Downloads`.

---

## Quick start — Android

### Install a prebuilt APK

1. Download the latest `.apk` from the [Releases](../../releases) page.
2. Open it on your phone and allow *Install unknown apps* if asked.
3. Grant the storage/media permission when the app asks.

> **Heads-up:** Google Play Protect may offer to scan the app because it isn't distributed through the Play Store. That's normal for any sideloaded app. The full source is in this repo, so you can also build it yourself.

### Build it yourself

Build environment: Ubuntu 24.04 (or Windows with WSL2), Buildozer, python-for-android.

```bash
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip python3-venv \
  autoconf automake libtool pkg-config zlib1g-dev libncurses-dev \
  libffi-dev libssl-dev build-essential cmake ccache

python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade buildozer "cython==0.29.36" setuptools

buildozer -v android debug          # APK ends up in ./bin/
```

Recommended `buildozer.spec` settings:

```ini
requirements = python3,kivy==2.3.0,yt-dlp,certifi
android.archs = arm64-v8a
android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,READ_MEDIA_VIDEO,READ_MEDIA_AUDIO
```

The app only uses Kivy, yt-dlp and certifi, so a short `requirements` line builds faster and breaks less. Pin `p4a.branch` to a release tag instead of `master` for reproducible builds.

---

## How to use it

1. **Paste** links with the *Paste* button (or type them). Several links at once is fine, and tracking junk like `?si=...` is cleaned automatically.
2. Pick **Video** or **Audio**, then a quality or audio format.
3. Tick **Full Playlists** if you want playlist links expanded.
4. Tap **+ Add Queue** to collect links, or go straight to **Start Batch Download**.
5. Watch progress, speed, and ETA per item. **Stop** cancels the batch.
6. Find your files in `Download/YouTube_Downloads` (Android) or `~/Downloads/YouTube_Downloads` (desktop).

Something stopped working? Tap **↻ Update Engine**. YouTube changes often, and a fresh yt-dlp fixes most breakage.

---

## Under the hood

For contributors and the curious. Everything lives in `main.py`.

- **Engine:** yt-dlp, with a progress hook feeding a thread-safe UI queue.
- **Client rotation:** tries `android`, `web_embedded`, `android + web_embedded`, `tv + mweb`, then `default`. It deliberately avoids the `android_vr` and `ios` clients, which return 403.
- **Format chains:** audio chains never fall back to a video format. Video quality is a hard `height<=N` cap. With FFmpeg, H.264 video and AAC audio are merged to MP4. Without it, only single-file streams are used.
- **Storage:** each task downloads into its own staging folder, then is exported to public Downloads with `shutil.copyfile` (not `copy2`, which fails on Android's FUSE storage), falling back to the MediaStore API. Partial downloads are kept so a stopped batch can resume.
- **Concurrency:** two downloads at a time, to stay friendly with YouTube's rate limits.
- **Kivy gotchas handled:** `[brackets]` in video titles are escaped so BBCode markup can't swallow them.

---

## Known limitations

- **No FFmpeg in the Android build yet**, so on the phone MP3/WAV fall back to M4A and video is limited to single-file streams (usually up to 360p).
- **Play Protect scan prompt** on sideloaded installs. It's per-APK, so every new build asks again.
- **YouTube changes break things.** That's the nature of the platform. Use **Update Engine** first, then open an issue with the log text.
- **YouTube only.** Other sites aren't a goal for this app.

---

## Roadmap / help wanted

- [ ] Bundle a working FFmpeg in the Android APK (unlocks HD merging, MP3, WAV on phones)
- [ ] Choose the format per queue item instead of per batch
- [ ] Signed release APKs built automatically with GitHub Actions
- [ ] Optional metadata and thumbnail embedding (needs FFmpeg)

---

## Contributing

Contributions are welcome — bug reports with the log text from the app are especially useful.

1. Fork the repo and create a branch.
2. Keep changes focused, and run the app on desktop before opening a PR (`python main.py`).
3. If you touch format selection, test both with and without FFmpeg.
4. Open a pull request describing what changed and how you tested it.

---

## Responsible use

This project is a front end for yt-dlp and is **not affiliated with or endorsed by YouTube or Google**. Downloading content may violate YouTube's Terms of Service or copyright law depending on the video and where you live. Use it for content you own, content with a license that allows it (such as Creative Commons or public domain), or other uses permitted where you are. You are responsible for how you use it. The software is provided as-is, with no warranty.

---

## Acknowledgements

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — the engine that does the real work
- [Kivy](https://kivy.org/) — the cross-platform UI
- [Buildozer](https://github.com/kivy/buildozer) and [python-for-android](https://github.com/kivy/python-for-android) — packaging Python into an APK
- [FFmpeg](https://ffmpeg.org/) — merging and conversion where available

---

## License

Released under the [MIT License](LICENSE).

If this saved you time, a ⭐ on the repo helps others find it.
