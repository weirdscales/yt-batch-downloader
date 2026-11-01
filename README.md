<div align="center">

# YT Batch Downloader

### Paste a pile of YouTube links. Pick a format. Walk away.

One project, two apps: a **desktop GUI** for Windows, macOS and Linux, and a **mobile app** for Android. Both are powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp), with no ads, no accounts, and no analytics.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Desktop](https://img.shields.io/badge/desktop-Tkinter-informational)
![Mobile](https://img.shields.io/badge/mobile-Kivy%20%7C%20Android-orange)
![Powered by yt-dlp](https://img.shields.io/badge/powered%20by-yt--dlp-red)

</div>

<!--
  Add screenshots here before publishing, e.g.:
  <p align="center">
    <img src="docs/desktop.png" width="520" alt="Desktop app">
    <img src="docs/mobile.png" width="220" alt="Mobile app">
  </p>
-->

---

## Pick your version

| | 🖥️ **Desktop** | 📱 **Mobile** |
|---|---|---|
| **Runs on** | Windows, macOS, Linux | Android 7.0+ |
| **Built with** | Python + Tkinter | Python + Kivy (packaged with Buildozer) |
| **Code** | [`desktop/main.py`](desktop/main.py) | [`mobile/main.py`](mobile/main.py) |
| **Get started** | [Install the requirements and run `main.py`](#quick-start--desktop) | [Download the APK from Releases](#quick-start--android) |
| **Format picker** | Best / 1080p / 720p / 480p, or Audio Only (MP3) | **Video** (Best / 1080p / 720p / 480p / 360p) or **Audio** (M4A / MP3 / WAV) |
| **FFmpeg** | Found automatically: your system install, or the bundled `imageio-ffmpeg` package | Not bundled in the APK yet (see [FFmpeg](#what-needs-ffmpeg)) |
| **Parallel downloads** | 1–6 workers (default 3) | 2 |
| **Add links by** | Paste, load a `.txt` file, or drag and drop | Paste |
| **Queue tools** | Retry failed/cancelled, remove selected, clear completed, overall progress bar | Per-item progress, **Clear Queue**, **Stop** |
| **Engine update** | One click runs `pip` to upgrade yt-dlp and FFmpeg | One tap hot-loads the latest yt-dlp from PyPI, no reinstall |
| **Saves to** | `~/Downloads/YouTube_Downloads` (changeable) | `Download/YouTube_Downloads` |

---

## Why YT Batch Downloader?

Many downloader apps handle one link at a time and wrap it in ads. This one is built for **batches**: paste ten links or a whole playlist, choose what you want once, and let it run.

- 📋 **True batch queue.** Paste any number of links at once, mixed with other text if you like. The app finds the URLs and removes duplicates.
- 📃 **Full playlist support.** Playlists expand into individual queue items, saved into a folder named after the playlist.
- ✅ **No fake "Completed".** An item is only marked done when the file is really on disk. Errors from yt-dlp are captured and shown in the log.
- 🛡️ **Built to survive YouTube's changes.** Both apps retry across several player-client profiles, and each has a built-in way to update the yt-dlp engine.
- 🎞️ **Plays everywhere.** The desktop app prefers H.264 + AAC MP4 instead of AV1, so files open in standard players.
- 📱 **Android storage done right.** The mobile app handles Android 11–14 Scoped Storage, including a MediaStore fallback.
- 🔍 **Open and auditable.** Each app is a single readable Python file. The only network traffic is YouTube (via yt-dlp) and PyPI when updating the engine.

---

## Quick start — Desktop

Works on Windows, macOS, and Linux. You need **Python 3.10 or newer**.

**1. Get the code and open the `desktop` folder**

```bash
git clone https://github.com/weirdscales/yt-batch-downloader.git
cd yt-batch-downloader/desktop
```

**2. Create a virtual environment (recommended)**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

On macOS and Linux, use `python3` if `python` isn't found.

**3. Install the requirements**

```bash
pip install -r requirements.txt
```

This installs three packages from [`desktop/requirements.txt`](desktop/requirements.txt):

| Package | What it's for |
|---|---|
| `yt-dlp[default]` | The download engine |
| `imageio-ffmpeg` | A bundled FFmpeg, so HD merging and MP3 conversion work without a separate install. If you already have FFmpeg on your PATH, the app uses that instead. |
| `tkinterdnd2` | Optional drag-and-drop. If it fails to install, delete that line from `requirements.txt`; the app works without it. |

**4. Run it**

```bash
python main.py
```

The Tkinter window opens and you're ready to paste links.

> **Tkinter missing?** It ships with Python on Windows and macOS (if it's missing, re-run the Python installer and tick *tcl/tk and IDLE*). On Linux install it separately: `sudo apt install python3-tk` (Debian/Ubuntu) or `sudo dnf install python3-tkinter` (Fedora).

### Using the desktop app

1. **Add links.** Paste them into the box, click **Paste Clipboard**, **Load .txt File...**, or drag a text file in.
2. Choose a **Format** and the number of **Workers**.
3. Set the options you want:
   - **Force H.264 + AAC (No AV1)** — on by default.
   - **Download Playlists** — on by default.
   - **Expand watch?v=...&list=** — also expand video links that carry a playlist.
   - **Playlist Subfolders** — one folder per playlist.
4. Click **+ Add URLs to Queue**, then **Download All Queued Videos**.
5. Watch progress, speed and ETA per row. Use **Retry Failed / Cancelled**, **Remove Selected** or **Clear Completed** to manage the queue, and **Stop / Cancel All** to halt everything.

If something stops working, click **↻ Update yt-dlp & FFmpeg Engine** first. YouTube changes often, and a fresh yt-dlp fixes most breakage.

---

## Quick start — Android

### Just want to use the app? Download the APK

The APK on the Releases page is already built and works. You don't need to build anything.

1. Go to the [**Releases**](../../releases) page and download the latest `.apk`.
2. Open it on your phone and allow *Install unknown apps* if asked.
3. Grant the storage/media permission when the app asks.

> **Heads-up:** Google Play Protect may offer to scan the app because it isn't distributed through the Play Store. That's normal for any sideloaded app. The full source is in this repo, so you can also build it yourself (see below).

### Using the mobile app

1. **Paste** links with the *Paste* button (or type them). Several links at once is fine, and tracking junk like `?si=...` is cleaned automatically.
2. Pick **Video** or **Audio**, then a quality or audio format.
3. Tick **Full Playlists** if you want playlist links expanded.
4. Tap **+ Add Queue** to collect links, or go straight to **Start Batch Download**. Whatever the selectors show when you press Start applies to everything still waiting.
5. Watch progress, speed and ETA per item. **Stop** cancels the batch.

If something stops working, tap **↻ Update Engine**. If every client hits a 403, the app also tries this once on its own.

### Want to modify the app or build your own APK?

**You only need this section if you want to change [`mobile/main.py`](mobile/main.py) (or the build settings) and make your own APK.** Otherwise, use the Releases APK above.

Android apps are built with [Buildozer](https://github.com/kivy/buildozer), which needs Linux. On Windows, use **WSL2 with Ubuntu** (24.04 works) and keep the project in your Linux home folder (for example `~/yt-batch-downloader`), not under `/mnt/c`, which is much slower.

> **Tip: try changes on your computer first.** The mobile app also runs on desktop: inside `mobile/`, run `pip install kivy yt-dlp certifi` and then `python main.py`. That's much faster than rebuilding the APK. Android-only parts (permissions, the MediaStore export) only run on a phone.

**1. Install the system packages (once)**

```bash
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip python3-venv \
  autoconf automake libtool pkg-config zlib1g-dev libncurses-dev \
  libffi-dev libssl-dev build-essential cmake ccache
```

**2. Set up the build environment (once)**

```bash
git clone https://github.com/weirdscales/yt-batch-downloader.git ~/yt-batch-downloader   # skip if you already have it
cd ~/yt-batch-downloader

python3 -m venv .venv                # keep this outside mobile/ so Buildozer never treats it as app source
source .venv/bin/activate
pip install --upgrade buildozer "cython==0.29.36" setuptools
```

**3. Build the APK**

```bash
cd ~/yt-batch-downloader/mobile
buildozer -v android debug 2>&1 | tee build.log
```

The first build downloads the Android SDK and NDK (several GB) and compiles everything, so it can take a long time. Later builds are much faster. When it finishes, your APK is in `mobile/bin/`.

**4. Rebuilding after changes (or when a build misbehaves)**

For a normal code change, just run step 3 again. If you changed `buildozer.spec` (requirements, python-for-android branch, architectures) or a build fails in strange ways, wipe the cached build state first:

```bash
source ~/yt-batch-downloader/.venv/bin/activate     # if it isn't already active
cd ~/yt-batch-downloader/mobile

buildozer android clean
rm -rf .buildozer/android/platform/python-for-android
rm -rf .buildozer/android/platform/build-*

pip install --upgrade buildozer "cython==0.29.36" setuptools
buildozer -v android debug 2>&1 | tee build.log
```

What those lines do:

| Command | Why |
|---|---|
| `buildozer android clean` | Removes the previous build's output |
| `rm -rf .buildozer/android/platform/python-for-android` | Forces a fresh python-for-android checkout (needed after changing `p4a.branch`) |
| `rm -rf .buildozer/android/platform/build-*` | Forces every Python package to be rebuilt from scratch |
| `pip install --upgrade ...` | Makes sure the build tools are current |
| `... \| tee build.log` | Saves the full log so you can search it if something fails |

`mobile/buildozer.spec` is already configured for this app. If you add Python libraries, list them under `requirements` in that file.

**5. If the build fails**

The last lines of the output only say `Command failed`. The real error is higher up. Search the log for it:

```bash
grep -n -iE "error|traceback" build.log | head -30
```

Open an issue and paste the lines around the first real error.

**6. Install your APK on a phone**

Copy `mobile/bin/*.apk` to your phone and open it. On WSL2 you can copy it to Windows first, for example `cp bin/*.apk /mnt/c/Users/<your-name>/Downloads/`. Or install over USB with `adb install -r yourapp-debug.apk`. Play Protect may ask to scan every new build, because each build is a new APK to it.

---

## Choose exactly what you get

### Desktop formats

| You pick | What you get |
|---|---|
| **Best Available / 1080p / 720p / 480p** | H.264 + AAC MP4, with the chosen resolution preferred |
| **Audio Only** | MP3 at 192 kbps via FFmpeg |

If a rare video exists only as AV1 or VP9, the app re-encodes it to H.264 with FFmpeg so it plays everywhere. That safety net can be slow.

### Mobile formats

| You pick | What you get |
|---|---|
| **Video → Best / 1080p / 720p / 480p / 360p** | H.264 + AAC MP4, capped at the height you chose |
| **Audio → M4A** | AAC audio in an `.m4a` file, no extra tools needed |
| **Audio → MP3** | 192 kbps MP3 (needs FFmpeg) |
| **Audio → WAV** | Uncompressed WAV (needs FFmpeg) |

### What needs FFmpeg?

Merging separate HD video and audio streams, and converting to MP3 or WAV, both need [FFmpeg](https://ffmpeg.org/). The desktop app finds or installs one for you (see above). The Android APK doesn't include one yet, so the mobile app checks for a working copy and **tells you in the log what it did instead**. It never quietly hands you something different.

| Mobile app | With FFmpeg | Without FFmpeg (Android APK today) |
|---|:---:|:---:|
| Video up to 360p | ✅ | ✅ |
| Video 480p – 1080p / Best | ✅ merged MP4 | ⚠️ best single-file stream (usually 360p) |
| Audio M4A | ✅ | ✅ |
| Audio MP3 / WAV | ✅ | ⚠️ saved as M4A |

> **Bundling FFmpeg into the Android APK** is the biggest open item on the [roadmap](#roadmap--help-wanted). The mobile app already looks for a binary shipped as `libffmpeg.so` and uses it if it runs. If you can help package one, see [Contributing](#contributing).

---

## Project layout

```text
yt-batch-downloader/
├── desktop/
│   ├── main.py                # Tkinter app (Windows / macOS / Linux)
│   └── requirements.txt       # pip install -r requirements.txt
├── mobile/
│   ├── main.py                # Kivy app (Android); Buildozer requires this file name
│   └── buildozer.spec
├── README.md
└── LICENSE
```

The two apps live in separate folders so Buildozer only packages the mobile code into the APK.

---

## Under the hood

For contributors and the curious. Each app is a single file.

**Shared**
- **Engine:** yt-dlp, with progress hooks feeding a thread-safe UI queue.
- **Honest status:** yt-dlp errors are captured by a custom logger, and a download only counts as done if files exist on disk.
- **URL handling:** links are extracted from free-form text and de-duplicated. The mobile app also strips tracking parameters such as `?si=` and `&pp=`.

**Desktop**
- Prefers `vcodec^=avc` and `acodec^=mp4a` in both the format string and the sort order, so YouTube's native H.264 + AAC streams are chosen over AV1.
- Falls back through mobile/TV player clients (and finally an unrestricted format list) when the default client fails.
- Worker threads run in a `ThreadPoolExecutor` sized by the **Workers** setting.

**Mobile**
- **Client rotation:** tries `android`, `web_embedded`, `android + web_embedded`, `tv + mweb`, then `default`. It deliberately avoids the `android_vr` and `ios` clients, which return 403 there.
- **Format chains:** audio chains never fall back to a video format. Video quality is a hard `height<=N` cap.
- **Storage:** each task downloads into its own staging folder, then is exported to public Downloads with `shutil.copyfile` (not `copy2`, which fails on Android's FUSE storage), falling back to the MediaStore API. Partial downloads are kept so a stopped batch can resume.
- **Concurrency:** two downloads at a time, to stay friendly with YouTube's rate limits.
- **Kivy gotchas handled:** `[brackets]` in video titles are escaped so BBCode markup can't swallow them.

---

## Known limitations

- **No FFmpeg in the Android build yet**, so on the phone MP3/WAV fall back to M4A and video is limited to single-file streams (usually up to 360p).
- **The desktop format list is shorter** than the mobile one: a single Audio Only option (MP3), and no 360p.
- **Play Protect scan prompt** on sideloaded Android installs. It's per-APK, so every new build asks again.
- **YouTube changes break things.** That's the nature of the platform. Use the update button first, then open an issue with the log text.
- **YouTube only.** Other sites aren't a goal for this project.

---

## Roadmap / help wanted

- [ ] Bring the **Video / Audio + M4A / MP3 / WAV** picker to the desktop app
- [ ] Bundle a working FFmpeg in the Android APK (unlocks HD merging, MP3, WAV on phones)
- [ ] Choose the format per queue item instead of per batch
- [ ] Signed release APKs built automatically with GitHub Actions
- [ ] Optional metadata and thumbnail embedding (needs FFmpeg)

---

## Contributing

Contributions are welcome. Bug reports with the log text from the app are especially useful.

1. Fork the repo and create a branch.
2. Keep changes focused, and run the app you changed before opening a PR.
3. If you touch format selection, test both with and without FFmpeg.
4. Open a pull request describing what changed and how you tested it.

---

## Responsible use

This project is a front end for yt-dlp and is **not affiliated with or endorsed by YouTube or Google**. Downloading content may violate YouTube's Terms of Service or copyright law depending on the video and where you live. Use it for content you own, content with a license that allows it (such as Creative Commons or public domain), or other uses permitted where you are. You are responsible for how you use it. The software is provided as-is, with no warranty.

---

## Acknowledgements

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — the engine that does the real work
- [FFmpeg](https://ffmpeg.org/) and [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) — merging and conversion
- [Kivy](https://kivy.org/) — the mobile UI
- [Buildozer](https://github.com/kivy/buildozer) and [python-for-android](https://github.com/kivy/python-for-android) — packaging Python into an APK
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) — optional drag-and-drop on desktop

---

## License

Released under the [MIT License](LICENSE). Copyright © 2026 weirdscales.

If this saved you time, a ⭐ on the repo helps others find it.
