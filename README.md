# mst3k-anything

> Paste a video URL. Get back a robot-heckled version, perfectly timed against the
> original audio — with a procedural theater strip along the bottom.

![current main UI — dense riff controls and resolved Writer/Judge models](docs/shots/ui-main.png)

**What it does.** Downloads a video, builds a dense plan of potential riff cues from
cadence, visual changes, audio energy, and natural pauses, transcribes the speech,
figures out what's on screen and what led into each cue, asks an LLM to write
context-specific jokes and callbacks, synthesizes them with PocketTTS in two voices,
sidechain-ducks the original audio, and mixes the riffs in. Dialogue overlap is an
intentional option; timing windows guide the landing rather than vetoing a good joke.
You watch the finished result in a synchronized original-versus-riffed player with a
side-by-side comparison handle.

![current provider picker — multimodal model catalogs for writer and judge](docs/shots/ui-providers.png)

![current player and riff editor](docs/shots/ui-player.png)

## Example output

[![Deadwood Relentless example](docs/examples/deadwood-relentless/poster.jpg)](docs/examples/deadwood-relentless/deadwood-relentless-riffed.mp4)

The [`Deadwood` Relentless example](docs/examples/deadwood-relentless/) is a complete
4:50 run using density bias `4`: 27 planned cues, 27 rendered riffs, and 8 judge
rewrites. The generated MP4, SRT, and final `riffs.json` manifest are included for
inspection and download.

## Informal model notes

From the multimodal models tested so far, the working impression is that **GPT-5.6
Luna** (`openai/gpt-5.6-luna`, via OpenRouter) writes the strongest lines and lands
jokes most reliably. **GLM-5.3 Flash** (`z-ai/glm-5.3-flash`) has been second-best
overall, while **Qwen 3.8 Flash** and **Kimi-k3-fast** have been useful but generally
weaker on comic turns and timing. This is an informal observation from our runs, not a
controlled benchmark; model choice, prompt/cache state, and source material can change
the result.

## Highlights

- **Context-sensitive riffing** — the writer receives timestamped *pre* and *mid* frames
  for each cue plus transcript completed through the cue. A whole-video analyst profile
  supplies continuity, motifs, and callback candidates; prompts prohibit using later
  reveals as if the audience has seen them.
- **Dense, evidence-first cueing** — cadence keeps the show alive even over continuous
  dialogue; silence, quietness, scene changes, audio energy, and visual signals shape
  cue scoring rather than acting as hard gates. Riff density ranges from Sparse through
  Relentless.
- **CPU-safe long-form ASR** — Parakeet processes audio in bounded 60-second worker
  chunks with per-chunk cache files, so long videos do not feed one unbounded offline
  decode stream and exhaust host memory.
- **Graceful edge cases** — short clips get a proportional lead-in, video-only inputs
  produce riffs over generated silence, and corrupt/stale cache artifacts are rejected
  rather than mixed into a new job.
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
- **Multi-provider LLM** — pick Hyper, Neuralwatt, or OpenRouter; each provider can
  expose a high-context multimodal model catalog for both Writer and Judge, while blank
  Hyper/Neuralwatt overrides fall back to their `.env` defaults.
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
| Voices | PocketTTS | built-in voices (alba/jane) by default; optional CLI custom reference conditioning via `VOICE_REF` |
| Mix | ffmpeg sidechaincompress + overlay | animated theater via static PNG (default) |
| Service | FastAPI + uvicorn + SQLite | the web UI / job queue |

## Quick start

```bash
# prerequisites: ffmpeg, yt-dlp, an ASR model (see docs/PLAN.md)
python3 -m venv web-venv
web-venv/bin/pip install fastapi uvicorn
uv venv asr-venv && uv pip install --python asr-venv/bin/python sherpa-onnx numpy
uv venv tts-venv && uv pip install --python tts-venv/bin/python pocket-tts
# Put Parakeet files at models/parakeet-ctc/model.int8.onnx and tokens.txt.

# configure a provider (or use the WebUI picker)
cp .env.example .env
# edit .env and add the provider API key/model

PYTHONPATH=src python -m mst3k.cli render "https://www.youtube.com/watch?v=VIDEO_ID" --out out/
```

### Optional custom voice (CLI/global configuration for now)

PocketTTS voice cloning is not exposed in the WebUI yet, but the CLI can use a local,
consented reference recording through `VOICE_REF`. The gated [PocketTTS model
page](https://huggingface.co/kyutai/pocket-tts) is CC-BY-4.0 and requires accepting its
current access/acceptable-use conditions, including the requested contact-information
sharing and explicit lawful consent for voice cloning. Before the first clone, sign in
there and authenticate the environment that owns `pocket-tts`:

```bash
tts-venv/bin/hf auth login
tts-venv/bin/hf auth whoami
```

That is Hugging Face authentication; `uv` created the environment but does not replace
`hf auth login`. The upstream PocketTTS instructions also show `uvx hf auth login`;
using the project-local executable keeps the token in the same user-level Hugging Face
cache. Do not commit the token or put it in `.env`.

PocketTTS's [`export-voice` command](https://github.com/kyutai-labs/pocket-tts/blob/main/docs/CLI%20Commands/export_voice.md)
processes the first 30 seconds of the reference and writes a reusable `.safetensors`
conditioning state, so use a clean, representative sample.

Precompute a reusable conditioning state:

```bash
PYTHONPATH=src python -m mst3k.cli prepare-voice \
  /absolute/path/to/consented-reference.wav \
  --out /absolute/path/to/my-riffer.safetensors
```

Then set `VOICE_REF` in `.env` to either that `.safetensors` file or the original WAV
(the latter is exported automatically to the local voice cache on first CLI render):

```bash
VOICE_REF=/absolute/path/to/my-riffer.safetensors
VOICE_PITCH=0.0   # semitone offset applied after conditioning
VOICE_RATE=1.0    # delivery-rate multiplier
```

The built-in pool is also colored: Alba is neutral and Jane is currently +2 semitones.
`VOICE_PITCH` adds a global offset to either built-in voice or a custom reference, while
`VOICE_RATE` scales delivery speed. Writer emphasis marks and fit-related tempo changes
are applied afterward. If the built-ins feel too bright/robotic, try `VOICE_PITCH=-1.0`
and `VOICE_RATE=0.96`; a custom voice starts from its own recording and receives only
those configured/output-stage transforms. Custom voice selection is currently CLI/.env
based. For a one-off CLI render, the same settings are available as `--voice-ref`,
`--voice-pitch`, and `--voice-rate` flags instead of editing `.env`. WebUI upload,
consent, previews, and per-job voice controls are planned rather than implemented.


The web service runs under systemd (see `deploy/mst3k-anything.service`). The CLI
also works directly; the repository is not currently packaged as an installable
console script.

## Repo layout

```
src/mst3k/        pipeline modules (ingest, analyze, transcribe, context,
                  understand, writer, voice // tts, mix, llm, providers)
app/              FastAPI service + static UI (index.html)
deploy/           systemd unit
demo/, jobs/      runtime artifacts (git-ignored)
models/           ASR model weights (git-ignored)
docs/             PLAN, README shots, and the completed Deadwood example
```

## License

MIT (LICENSE), with `NOTICE` for upstream credits.
