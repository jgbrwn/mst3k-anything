# mst3k-anything

> Paste a video URL. Get back a robot-heckled version, perfectly timed against the
> original audio — with the two-bot theater sitting at the bottom.

![screenshot — main UI](docs/shots/ui-main.png)

**What it does.** Downloads a video, listens for natural pauses, transcribes the speech,
figures out what's on screen just before/after each pause, asks an LLM to write
MST3K-style jokes for those exact moments, synthesizes the jokes with PocketTTS
in two voices (Alba + Jane), ducks the original audio dynamically, and mixes the
riffs in. You watch in a side-by-side player that lets you drag to compare the original
versus the riffed pass.

![screenshot — provider picker](docs/shots/ui-providers.png)

![screenshot — side-by-side player](docs/shots/ui-player.png)

## Highlights

- **Context-sensitive riffing** — the writer doesn't just see a frame at the gap start.
  Each riff candidate gets a *bundle*: transcript before/after, frames at T-3 / T / T+3,
  hot-moment markers from the audio, and (for callbacks) the *full* transcript so a riff
  at 2:00 can refer to something said at 0:15.
- **Silence-true placement** — riffs only go where ffmpeg actually detects quiet audio
  or where a paired silence listener says there's a window. No more riffs stepped on
  by dialogue.
- **Two-voice ensemble** — Alba (70%) + Jane (30%) by default, routed deterministically
  by the line so re-rendered versions sound consistent. Expressiveness hints in the
  written line (`*word*`, trailing `...`, `!`, `?`) become audio coloration.
- **Real-time log** — after submitting a URL the UI immediately shows a console
  tailing the pipeline stage-by-stage. When done the video player appears.
- **Edit + re-render** — open a finished job, edit the riffs.json, hit re-render; only
  the TTS + mix re-runs (the expensive transcription/analysis stages cache).
- **Multi-provider LLM** — pick Hyper, Neuralwatt, or OpenRouter (full
  OpenRouter high-context multimodal picker included); per-job selectable.

## Stack

| Layer | Tech | Notes |
|---|---|---|
| Ingest | yt-dlp | YouTube, archive.org, direct mp4 links |
| Audio analysis | ffmpeg (silencedetect + astats) | gap detection, hot moments |
| Transcription | sherpa-onnx + Parakeet 110M INT8 | CPU-only, RTF 0.05 |
| Video analysis | ffmpeg frame grabs + signalstats | shot context, luma variance |
| Comedy brain | any OpenAI-compatible chat-completions API | system prompt templates per content kind |
| Voices | PocketTTS | built-in voices (alba/jane by default) |
| Mix | ffmpeg sidechaincompress + overlay | animated theater via static PNG (default) |
| Service | FastAPI + uvicorn + SQLite | the web UI / job queue |

## Quick start

```bash
# prerequisites: ffmpeg, yt-dlp, ASR model files (see docs/PLAN.md)
python3 -m venv web-venv && pip install -r web-venv-required.txt  # fastapi uvicorn
uv venv asr-venv && uv pip install sherpa-onnx numpy
uv venv tts-venv && uv pip install pocket-tts
# voice/asr model blobs land in models/ — the pipeline downloads the first time.

echo "LLM_API_KEY=sk-..." > .env   # or set per-job via the UI picker
PYTHONPATH=src mst3k render "https://www.youtube.com/watch?v=VIDEO_ID" --out out/
```

The web service runs under systemd (see `deploy/mst3k-anything.service`). The CLI
also works directly.

## Repo layout

```
src/mst3k/        pipeline modules (ingest, analyze, transcribe, context,
                  understand, writer, voice // tts, mix, llm, providers)
app/              FastAPI service + static UI (index.html)
deploy/           systemd unit
demo/, jobs/      runtime artifacts (git-ignored)
models/           ASR model weights (git-ignored)
docs/             PLAN, shots for this README
```

## License

MIT (LICENSE), with `NOTICE` for upstream credits.
