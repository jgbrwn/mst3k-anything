# mst3k-anything

> Paste a video URL. Get back a robot-heckled version, perfectly timed against the
> original audio — with the two-bot theater sitting at the bottom.

![current main UI — dense riff controls and resolved Writer/Judge models](docs/shots/ui-main.png)

**What it does.** Downloads a video, builds a dense plan of potential riff cues from
cadence, visual changes, audio energy, and natural pauses, transcribes the speech,
figures out what's on screen and what led into each cue, asks an LLM to write
context-specific jokes and callbacks, synthesizes them with PocketTTS in two voices,
sidechain-ducks the original audio, and mixes the riffs in. Dialogue overlap is an
intentional option; timing windows guide the landing rather than vetoing a good joke.
You watch in a side-by-side player that lets you drag to compare the original versus the
riffed pass.

![current provider picker — OpenRouter writer and judge models](docs/shots/ui-providers.png)

![current player and riff editor](docs/shots/ui-player.png)

## Highlights

- **Context-sensitive riffing** — the writer doesn't just see a frame at the gap start.
  Each riff candidate gets a *bundle*: transcript before/after, frames at T-3 / T / T+3,
  hot-moment markers from the audio, and (for callbacks) the *full* transcript so a riff
  at 2:00 can refer to something said at 0:15.
- **Dense, evidence-first cueing** — cadence keeps the show alive even over continuous
  dialogue; silence, quietness, scene changes, audio energy, and visual beats improve
  cue selection rather than acting as hard gates.
- **CPU-safe long-form ASR** — Parakeet processes audio in bounded 60-second worker
  chunks with per-chunk cache files, so long videos do not feed one unbounded offline
  decode stream and exhaust host memory.
- **Real-time log** — after submitting a URL the UI immediately shows a console
  tailing the pipeline stage-by-stage. It follows the newest output, briefly allows
  manual scrolling, then returns to the live tail; failed-job logs remain visible.
  When done the video player appears.
- **Edit + re-render** — open a finished job, edit the final rendered riff manifest in
  the browser, hit re-render; the submitted manifest is used directly (no fresh LLM
  rewrite), while cached media analysis/transcription is retained.
- **Stable job lifecycle** — each API submission gets a private work directory, while
  the database slug becomes a human title slug after ingest. Repeated submissions of
  the same video cannot overwrite each other's logs, PIDs, or outputs.
- **Multi-provider LLM** — pick Hyper, Neuralwatt, or OpenRouter (full
  OpenRouter high-context multimodal picker included); per-job selectable.
- **Provider-resilient structured LLM calls** — empty content, structured content blocks,
  truncated JSON, and transient provider failures are retried for every provider. Provider-
  specific reasoning controls are applied only when known-supported or explicitly configured.

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
