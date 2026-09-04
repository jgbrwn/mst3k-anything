# Installation and first run

This is the **canonical setup guide** for running `mst3k-anything` locally on Linux,
macOS, or Windows. The normal path does not require Docker, `uv`, a GPU, or Python
package activation.

## What you need

- A 64-bit Python **3.10–3.14** installation; **Python 3.12** is the recommended choice.
- `ffmpeg` (which also supplies `ffprobe`).
- An internet connection for Python packages, the Parakeet ASR model, PocketTTS's first
  model download, and video URLs.
- An API key for at least one supported OpenAI-compatible vision model provider:
  Hyper, Neuralwatt, or OpenRouter.
- Several GB of free disk space for environments, model caches, downloaded source videos,
  and rendered outputs. CPU-only operation is supported; longer videos take longer.

If Python is missing, install Python 3.12 first:

| OS | Command / action |
|---|---|
| Ubuntu/Debian | `sudo apt-get install -y python3.12 python3.12-venv` |
| macOS | With [Homebrew](https://brew.sh/): `brew install python@3.12` |
| Windows 10/11 | In PowerShell: `winget install --id Python.Python.3.12 -e` |

The installer downloads the Parakeet TDT-CTC 110M INT8 asset from the
[sherpa-onnx model release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)
and verifies the expected file checksums before installing it.

The setup flow is intended for 64-bit Linux, macOS, and Windows. The automated smoke test
runs on Linux in this repository; the doctor will identify platform-specific wheel or tool
problems before a job is submitted.

The installer creates three isolated environments because ASR, TTS, and the WebUI have
large or different dependencies. You do not need to activate any of them manually.

## 1. Install ffmpeg

`ffmpeg` and `ffprobe` must be available on `PATH` before the first render.

| OS | Command / action |
|---|---|
| Ubuntu/Debian | `sudo apt-get update && sudo apt-get install -y ffmpeg` |
| Fedora/RHEL-like | Install `ffmpeg` using the distribution's enabled multimedia repository. |
| macOS | With [Homebrew](https://brew.sh/): `brew install ffmpeg` |
| Windows 10/11 | In PowerShell: `winget install --id Gyan.FFmpeg.Shared -e` |

Verify both tools:

```text
ffmpeg -version
ffprobe -version
```

If your binaries are not on `PATH`, set these in `.env` after the installer creates it:

```dotenv
MST3K_FFMPEG=/absolute/path/to/ffmpeg
MST3K_FFPROBE=/absolute/path/to/ffprobe
```

On Windows, use a Windows path such as
`MST3K_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe`.

## 2. Get the project

With Git:

```bash
git clone https://github.com/jgbrwn/mst3k-anything.git
cd mst3k-anything
```

Or download the repository ZIP from GitHub and open a terminal in the extracted
`mst3k-anything` directory. Git is not otherwise required by the application.

## 3. Run the installer

### Linux or macOS

```bash
./scripts/install.sh
```

If the checkout came from a filesystem that removed executable bits:

```bash
chmod +x scripts/*.sh
./scripts/install.sh
```

### Windows PowerShell

Run this only for the current PowerShell window; it does not change the machine policy:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\install.ps1
```

You can also run `install.cmd` from Command Prompt or Explorer.

The installer:

1. creates `web-venv`, `asr-venv`, and `tts-venv`;
2. installs FastAPI/Uvicorn/yt-dlp, sherpa-onnx/NumPy, and CPU-first PocketTTS;
3. downloads and verifies the Parakeet TDT-CTC 110M INT8 model into `models/`;
4. creates `.env` from `.env.example` without overwriting an existing `.env`; and
5. offers to configure a provider without echoing the API key.

To skip the interactive provider question:

```bash
./scripts/install.sh --no-configure       # Linux/macOS
.\scripts\install.ps1 --no-configure      # PowerShell
```

The installer may take a while, especially while downloading PyTorch and PocketTTS.
The first actual render can also download PocketTTS's model into the user Hugging Face
cache.

## 4. Configure an LLM provider

If you skipped configuration, run:

```bash
python3 scripts/configure.py    # Linux/macOS
py -3 scripts/configure.py      # Windows
```

Choose the provider and enter its key at the hidden prompt. OpenRouter also needs a
model ID such as `google/gemma-4-31b-it`; Hyper and Neuralwatt have configured defaults.
The WebUI can later select a different writer and judge model independently.

Manual configuration is also supported by copying `.env.example` to `.env` and editing
only the provider settings. Never commit `.env` or paste an API key into a browser field.
The key is used by the local backend, not sent to the WebUI as part of a job request.

## 5. Check the installation

Run the doctor before starting the service:

```bash
python3 scripts/doctor.py --strict    # Linux/macOS
py -3 scripts/doctor.py --strict      # Windows
```

Every `[FAIL]` item must be fixed. `[WARN]` usually means the installation is usable
but no provider key/model has been configured yet. The doctor checks Python, all three
virtual environments, imports, ffmpeg/ffprobe/yt-dlp, PocketTTS, the Parakeet files, and
writable job storage. It never prints API keys.

## 6. Start the WebUI

### Linux or macOS

```bash
./scripts/start.sh
```

### Windows PowerShell

```powershell
.\scripts\start.ps1
```

Or use `start.cmd` from Command Prompt. Open **http://127.0.0.1:8000** in your browser.
Keep the terminal open; press `Ctrl+C` to stop the server.

If port 8000 is already in use, choose another local port:

```bash
./scripts/start.sh --port 8765                 # Linux/macOS
.\scripts\start.ps1 --port 8765                # Windows
```

Then open `http://127.0.0.1:8765`. To make the service reachable from another device,
use `--host 0.0.0.0` only on a trusted network; the current WebUI has no authentication.

## 7. Make your first riffed video

1. Open the WebUI and choose the writer provider/model.
2. Optionally choose a different judge provider/model.
3. Choose a density from **Sparse** through **Relentless**.
4. Paste a YouTube URL, an archive.org item/direct video URL, another yt-dlp-supported
   URL, a direct video-file URL, or a local video path.
5. Submit and leave the WebUI open while the console shows ingest, transcription,
   understanding, cue planning, writing/judging, TTS, placement, and mixing.
6. When complete, play the synchronized original-versus-riffed result or download the
   MP4, SRT, and final riff manifest.

A first job is intentionally CPU-heavy. Short clips are the best smoke test. Video-only
inputs are supported; they receive an empty transcript and a generated riff audio bed.

## Optional CLI render

The WebUI is recommended, but the same pipeline can run directly:

```bash
# Linux/macOS
PYTHONPATH=src web-venv/bin/python -m mst3k.cli render \
  "https://www.youtube.com/watch?v=VIDEO_ID" --out out/

# Windows PowerShell
$env:PYTHONPATH = "src"
.\web-venv\Scripts\python.exe -m mst3k.cli render `
  "https://www.youtube.com/watch?v=VIDEO_ID" --out out
```

The CLI accepts `--voice-ref`, `--voice-pitch`, and `--voice-rate` for the optional
custom-reference voice workflow. The WebUI currently uses the configured built-in voice
pool and does not expose voice uploads.

## Troubleshooting

### `ffmpeg` or `ffprobe` not found

Install ffmpeg using the OS instructions above, reopen the terminal, and run the doctor.
If it is installed somewhere custom, set `MST3K_FFMPEG` and `MST3K_FFPROBE` in `.env`.

### `Parakeet model is missing`

Rerun the installer; the model download is safe to repeat:

```bash
python3 scripts/install.py              # Linux/macOS
py -3 scripts/install.py                # Windows
```

### Provider/model errors

Run the configure script again. OpenRouter model IDs must include the provider slash
(for example `google/gemma-4-31b-it`). The provider must support image input because the
writer and judge receive video frames.

### PowerShell refuses to run a script

Use the process-scoped command from step 3:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### A job is slow or appears idle

ASR, PocketTTS, and video encoding are local CPU work, while the writer/judge calls wait
on the provider. Watch the live stage log. Avoid submitting multiple long jobs on a small
machine; the current service intentionally has one worker.

### Port conflict

Start with another port, for example `scripts/start.sh --port 8765` or
`.\scripts\start.ps1 --port 8765`.

### Linux service installation

`deploy/mst3k-anything.service` is the VM-specific systemd unit used by the hosted demo
and contains that VM's user/path. Do not copy it unchanged to another computer. For a
personal Linux deployment, first get the foreground start script working, then create a
service using your own absolute project path and user. macOS and Windows use the start
scripts rather than this unit.
