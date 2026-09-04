# mst3k-anything — Architecture & plan

**Status snapshot: September 4, 2026.** This document describes the behavior that is
implemented today and separates it from the remaining roadmap. It is intentionally more
literal than the original prototype plan: the README and this file should describe the
same product.

## 1. Goal and current status

Paste a video URL or local video path and receive a finished video with original,
dry theater-commentator riffs, measured PocketTTS speech, ducked source audio, and a
procedural theater strip. Riffs are dense and context-specific, but intentional
dialogue overlap is allowed. Timing windows guide a landing; they do not veto a good
line merely because a source video is talking.

The current implementation is a CPU-first Python pipeline wrapped by a small FastAPI
service. It is not an MST3K character or voice impersonator: the personas, lines,
voices, and procedural silhouettes are original.

### Current milestone status

| Area | Status | Notes |
|---|---|---|
| M0 proof of concept | ✅ complete | End-to-end media, TTS, mix, and overlay were proven first in `demo/`. |
| M1 CLI pipeline | ✅ complete | `python -m mst3k.cli render` runs the staged pipeline with private artifacts. |
| M2 style framework | ⏳ future | No external-riff distillation job or checked-in `STYLE_GUIDE.md` yet. |
| M3 service/UI | ✅ mostly complete | FastAPI, SQLite state, one worker, SSE logs, history, player, editor, and rerender. |
| M4 comedy loop | ✅ partial | Dense cueing, profile, callbacks, judge/rewrite, causal local evidence, sidechain mix, and overlay exist. |
| M5 operations | 🔧 in progress | Cross-platform install/doctor/start helpers and VM systemd deployment work; auth, rate limits, packaging, and scaling remain. |

### Current examples

`docs/examples/deadwood-relentless/` contains three completed Deadwood runs as of
September 4, 2026. All use Relentless density (bias `4`) on the same roughly 4:50,
1280×720 source:

- Job 72 — anecdotal first place: OpenRouter `google/gemini-3.8-flash` writer and `deepseek/deepseek-v4-flash-vision-exp` judge; 27/27 rendered and all 27 retained without rewrites.
- Job 62 — anecdotal second place: OpenRouter `openai/gpt-5.6-luna` as writer and judge; 27/27 rendered, 8 judge rewrites.
- Job 63 — anecdotal third place: OpenRouter `google/gemma-4-31b-it` writer and `x-ai/grok-4.6` judge; 26/27 rendered, 23 judge rewrites (the final cue hit the video boundary).

The directory includes all three MP4s, their SRTs, final rendered manifests, one winner
poster, and a comparison README. Sol and Fable were not tested.

## 2. Architecture

```text
┌─────────────── static HTML/CSS/JS frontend ────────────────┐
│ submit · providers · density · SSE log · player · editor   │
└───────────────────────┬────────────────────────────────────┘
                        │ REST + Server-Sent Events
┌───────────────────────▼────────────────────────────────────┐
│ FastAPI service · SQLite durable job state                 │
│ in-memory queue.Queue · one worker · isolated child process │
└───────────────────────┬────────────────────────────────────┘
                        │ python -m mst3k.cli render
┌───────────────────────▼────────────────────────────────────┐
│ per-job work directory and resumable filesystem artifacts  │
│ ingest → ASR → profile → cues → context → write/judge     │
│        → PocketTTS/place → ffmpeg mix/deliver             │
└────────────────────────────────────────────────────────────┘
```

SQLite stores job status, provider/model labels, output paths, process identity, and
errors. Dispatch is currently an in-memory single-worker queue, not a distributed or
SQLite-native scheduler. Each API row receives a private `jobs/job-<id>-<slug>/`
work directory so repeated submissions cannot collide. The CLI can also use a URL-slug
workspace when run without the API.

The actual stage order is:

1. **Ingest** — normalize the input to `source.mp4` and write metadata.
2. **Transcribe** — chunked CPU Parakeet ASR, or an empty transcript for video-only input.
3. **Initial frames** — ten evenly sampled context frames for understanding.
4. **Understand** — profile content kind, premise, targets, motifs, and scene beats.
5. **Cue plan** — deterministic cadence plus audio/cut/quiet/silence candidates.
6. **Cue context** — cue frames, transcript context, and hot-moment markers.
7. **Write + judge** — batched drafts, omitted-cue repair, verdicts, and rewrites.
8. **Synthesize + place** — PocketTTS duration measurement and millisecond placement.
9. **Mix + deliver** — duck, overlay, encode, SRT, and final rendered manifest.

## 3. Implemented pipeline

### 3.1 Ingest

`src/mst3k/ingest.py` accepts:

- local paths and `file:` paths;
- YouTube URLs;
- archive.org item/direct URLs;
- direct URLs whose path has a recognized video extension; and
- other URLs through a yt-dlp generic-extractor attempt followed by a direct-download
  fallback.

yt-dlp requests video up to 720p, writes metadata, and asks for English subtitles.
Subtitles are retained in the job when found but are **not currently used** as the
transcript source; Parakeet remains authoritative. Inputs are validated with ffprobe,
limited to 2.5 hours, and normalized to `source.mp4`. A missing audio track is supported:
the job receives an empty transcript and the mix supplies an `anullsrc` riff bed.

### 3.2 Transcription

Parakeet CTC 110M INT8 runs through sherpa-onnx in a separate `asr-venv`, CPU-only,
with bounded 60-second subprocess chunks. Chunk JSON is cached independently, so an
interrupted long job can resume without decoding the entire recording again. The
published artifact is `transcript.json` with timestamped lines, tokens, and timestamps;
`transcript_raw.json` is retained for diagnostics.

### 3.3 Understanding and visual context

The initial frame pass stores ten evenly spaced `ctxNN.png` frames. The understanding
call receives those frames, metadata, and up to 24,000 characters of timestamped
transcript, and returns a compact content profile:

```text
kind · tone · premise · characters · targets · running_gags · visual_motifs
scene_beats · do_not_target · style_guide
```

The profile call is bounded and JSON-repaired. If the provider fails, the job continues
with an evidence-only profile and a title-based fallback kind such as `vlog` or `movie`.
The profile is a whole-video continuity aid, not permission to predict. Its future scene
beats and motifs are still visible to the writer as analyst metadata; prompts tell the
writer to use a detail only once its evidence timestamp has occurred. A future hardening
item is to project the profile itself causally per cue rather than relying on that rule.

After cue selection, each cue gets an anchor frame and a separate context bundle with
only `pre` (anchor −2.5s) and `mid` (anchor) frames. The writer and judge receive
transcript completed through the cue, plus local overlapping setup where appropriate;
post-cue transcript and future frames are retained only for internal diagnostics and
are not supplied as writing evidence.

### 3.4 Dense cue planning

`analyze.find_gaps()` always creates a cadence baseline. It then ranks possible
replacements from:

- detected pauses/silence;
- quiet and RMS windows;
- hot audio windows; and
- scene cuts.

A per-cue visual-interest/luma score is recorded from the cue frames after the plan is
selected for downstream placement/debugging; it is not currently a hard cue-selection
gate. Silence and quietness are signals, not eligibility gates. A small spacing rule only
deduplicates near-identical anchors. The UI exposes five levels: **Sparse**, **Light**,
**Lively**, **Dense**, and **Relentless**. The current multipliers are approximately
`0.55`, `0.78`, `1.0`, `1.4`, and `1.8` against the content-kind baseline, with an
emergency ceiling of 400 riffs. `TARGET_RIFF_COUNT`, when supplied by code, acts as a
cap rather than a promise that post-processing will drop lines to fit.

### 3.5 Writing and judging

The writer is an original dry theater commentator. Its prompt emphasizes:

- concrete visual/spoken/editing evidence;
- a real comic turn rather than description;
- compact fragments, contractions, underreaction, irony, and varied mechanisms;
- callbacks only to details already established; and
- natural audience timing, including explicitly marked setup overlap.

Normal writer batches contain six cues. Structured output is normalized manually: cue
IDs must be known and unique, lines must be nonempty and bounded, mechanisms/timing are
validated, and evidence is limited to supplied references. If a provider omits cues,
the missing IDs receive a constrained recovery call. Results are cached per job with a
policy marker and retained as `drafts.json`.

The judge runs in the same six-item batches unless a job overrides that setting. It
scores groundedness, comic turn, voice, and timing; rewrites salvageable weak lines one
at a time; and drops only when no grounded joke remains. `judged_riffs.json` is a
work/debug artifact. The public `riffs.json` is written only after synthesis and
contains only placements that actually reached the mixer.

### 3.6 PocketTTS, custom voice conditioning, and placement

PocketTTS generates local speech. The default pool is two built-in voices (`alba` and
`jane`) with deterministic per-riff assignment. The current voice pipeline has two
modes:

- **Built-in mode:** pool-specific pitch/rate coloring is applied, then global
  `VOICE_PITCH` (semitones) and `VOICE_RATE` (multiplier) are applied. The current
  defaults are Alba at 0 semitones and Jane at +2 semitones. Writer emphasis marks and
  timing-fit tempo changes add small output-stage effects afterward.
- **Custom-reference mode:** setting `VOICE_REF` switches to one local custom voice
  instead of randomly selecting Alba/Jane. It accepts a WAV reference or a PocketTTS
  `.safetensors` conditioning state. A WAV is converted once with PocketTTS's
  `export-voice` command into `~/.cache/mst3k-anything/voices/`; the resulting state is
  reused by later riffs/jobs. The current entry point is the CLI or global `.env`, not
  the WebUI. One-off renders also accept `--voice-ref`, `--voice-pitch`, and
  `--voice-rate`.

`python -m mst3k.cli prepare-voice SOURCE --out STATE.safetensors` performs that
conversion explicitly. The reference must be lawfully usable and consented for voice
conditioning. PocketTTS's export path uses the first 30 seconds of the reference, so a
clean, representative sample is preferred. PocketTTS model access/authentication is a
prerequisite for a first custom-voice export; the setup steps are in the README. The
application does not clone named performers or characters.

TTS outputs are measured with ffprobe and cached under `tts/` using text, voice,
pitch/rate, reference-file, and delivery-hint inputs.

The preferred cue envelope and word budget guide delivery but do not reject a good line.
A modest tempo stretch may help a button; a longer riff is intentionally allowed to run
over source dialogue. Ordinary reactions use a default 0.35-second delay after the
anchor. Negative offsets are reserved for explicitly marked intentional overlap/setup.
Placement clamps only to the physical video start/end and uses integer-millisecond
`adelay`; a late line is not moved backward merely to preserve its full tail.

### 3.7 Mix and delivery

`mix.py` builds an ffmpeg graph with:

- a riff bus mixed from the measured speech files;
- sidechain compression to duck the source while riffs speak;
- a limiter for dense sections; and
- a static procedural RGBA theater strip by default, or an optional short animated
  WebM overlay.

Normal renders re-encode the video with libx264 because the overlay/filter graph is
active; this is not a stream-copy path. The job emits:

- final `<title>_riffed.mp4` and `final.mp4`;
- `<title>_riffs.srt` and `riffs.srt`, representing actual riff audio spans; and
- `riffs.json`, the final rendered placement manifest.

The WebUI serves both the original `source.mp4` and the final video, so its synchronized
comparison player is genuinely original-versus-riffed. It also serves MP4/SRT downloads
and an editable manifest; drafts, judge output, caches, and source media stay in the
private job directory.

## 4. LLM providers and structured-response policy

The client speaks OpenAI-compatible `/chat/completions` endpoints. The configured
providers are:

| Provider | Default model | Current role |
|---|---|---|
| Hyper | `qwen3.8-flash` | default writer/judge/understanding route |
| Neuralwatt | `kimi-k3-fast` | selectable writer/judge/understanding route |
| OpenRouter | explicit `provider/model` | selectable model picker, including multimodal models |

Hyper and Neuralwatt expose OpenAI-compatible `/models` catalogs with differing metadata
shapes; the backend normalizes them and filters to high-context models advertising vision.
If a provider cannot publish usable capabilities, the UI still leaves the override as a
free-text field and blank values retain the configured default. OpenRouter keeps its
existing specialized high-context multimodal catalog.


Structured-call recovery is provider/model agnostic:

- transient 429/502/503/504 and network failures retry with backoff;
- text content, structured content blocks, and common reasoning fields are extracted;
- empty final content gets a compact JSON-only repair turn; and
- malformed or truncated JSON gets one compact repair turn before the caller decides
  whether to degrade gracefully.

Reasoning controls are different: parameter names are not universal. The client sends
`reasoning_effort=low` and `include_reasoning=false` automatically only for the known
OpenRouter `z-ai/*` GLM family. `MST3K_REASONING_EFFORT` is an explicit opt-in for any
provider/model whose gateway documents those fields; unsupported fields are never sent
by default.

### Informal model observation

This is not a controlled benchmark. The earlier working impression favored **GPT-5.6
Luna** (`openai/gpt-5.6-luna`) for writing and joke landing, with **GLM 5.3 Flash**
(`z-ai/glm-5.3-flash`) next and **Qwen 3.8 Flash**/ **Kimi-k3-fast** generally weaker
in those runs. The latest Deadwood comparison makes the **Gemini Flash 3.8 writer +
DeepSeek V4 Flash Vision judge** the current anecdotal winner: Gemini produced deeper,
funnier, context-aware turns, while DeepSeek retained all 27. That pair was roughly 4–5×
more expensive than the value-oriented combinations. Sol and Fable were not tested.

Job 72 is the Gemini/DeepSeek reference (27/27 rendered, no rewrite requests); Job 62 is
the Luna reference (27/27 rendered, 8 judge rewrites); Job 63 is the Gemma/Grok reference
(26/27 rendered, 23 judge rewrites). The ranking is only a model-pair observation on this
clip. A future fixed-clip A/B benchmark should record human ratings, judge scores,
rewrite/drop rates, causal-reference errors, latency, and cost before treating any
ordering as a measurement.

## 5. Cache and reliability model

Caches are primarily **per-job filesystem caches**, not a shared content-addressed store.
`file_signature()` uses resolved path, size, and modification time; policy markers also
include relevant versions, cue anchors, source/audio signatures, and selected settings.
Important artifacts include:

```text
source.mp4, meta.json
transcript.json, transcript_raw.json, asr_chunks/
frames/, frames_policy.json, context_frames_policy.json
profile.json, profile_policy.json
gaps.json, gaps_policy.json, audio_windows.json, cuts.json
bundles.json, drafts.json, drafts_policy.json, judged_riffs.json
tts/, ~/.cache/mst3k-anything/voices/
theater.png or theater_anim.webm
final.mp4, riffs.srt, riffs.json
```

Corrupt JSON, zero-duration TTS, stale source/audio/frame policy, and invalid editor
manifests are rejected or regenerated. Known limitations remain: profile/draft policy
markers do not yet fully encode provider/model identity, and editor rerender currently
clears render-dependent TTS/segment directories rather than preserving every unchanged
speech file. Both are backlog items, not guarantees of the current cache behavior.

## 6. Service and frontend

The service is a single FastAPI application served by uvicorn. New local installations use
`scripts/start.sh`, `scripts/start.ps1`, or `start.cmd` and listen on `127.0.0.1:8000` by
default. The hosted VM additionally uses `deploy/mst3k-anything.service` on port 8000.
The frontend is one static vanilla HTML/CSS/JavaScript file, not React, Vite, or htmx.

Implemented UI behavior:

- URL submission;
- writer provider/model selection;
- optional separate judge provider/model;
- OpenRouter model discovery plus normalized Hyper/Neuralwatt multimodal model catalogs and custom model IDs;
- Sparse → Relentless density selection;
- SSE stage log/progress with manual-scroll recovery;
- history, view, hide, cancel, and delete;
- original-versus-riffed synchronized comparison player;
- resume position, MP4/SRT downloads, and Escape-to-close playback; and
- browser editing of `riffs.json` followed by a cheap exact-manifest rerender.

Not currently exposed in the UI are arbitrary voice-set, snark, duck-depth, overlay,
or upload controls. There is no live per-riff preview list or community gallery. The API
uses one in-memory worker, so production multi-user scheduling needs a later redesign.

## 7. Development and operation

Required local components are:

- ffmpeg and ffprobe;
- Python 3.10–3.14 (Python 3.12 recommended);
- the three environments created by `scripts/install.*`:
  `web-venv` with FastAPI/Uvicorn/yt-dlp, `asr-venv` with sherpa-onnx/NumPy, and
  `tts-venv` with CPU-first PyTorch/PocketTTS; and
- the verified `models/parakeet-ctc/model.int8.onnx` plus `tokens.txt` files downloaded
  by the installer.

The repository is intentionally run from its checkout rather than published as a wheel.
The direct CLI form is documented in [`docs/INSTALL.md`](INSTALL.md):

```bash
PYTHONPATH=src web-venv/bin/python -m mst3k.cli render \
  "https://www.youtube.com/watch?v=VIDEO_ID" --out out/
```

Optional custom-reference setup follows the same steps as the README: accept the gated
conditions on the [PocketTTS model page](https://huggingface.co/kyutai/pocket-tts), run
`hf auth login` using the executable in `tts-venv/bin/` or `tts-venv/Scripts/`, then use
`prepare-voice` or `VOICE_REF`. Authentication is local to the Hugging Face CLI; the
browser UI must never receive the token.

The hosted VM receives provider secrets from `.env`/systemd. Per-job provider and model
choices are stored in SQLite and passed to the child process through environment
variables. On POSIX systems, job cancellation uses a private process group; on Windows
it uses `taskkill /T` for the API-owned process tree. The VM deployment and exe.dev proxy
are working; authentication, rate limiting, cleanup policy, and horizontal workers remain
operational follow-up work.

## 8. Style framework and legal boundary

The current craft framework lives in code prompts and the content-kind register. A
separate `STYLE_GUIDE.md`, few-shot example bank, and automated distillation pipeline
from reference riff material do not exist yet.

If that work is added, it must distill timing and joke mechanisms rather than copy lines,
characters, catchphrases, footage, or voices. The project should keep original personas
and use public-domain or properly licensed, consented reference audio for current custom
voice conditioning and any future cloning workflows.
Users are responsible for the rights and terms of source videos; the checked-in
Deadwood example is a generated demonstration of the pipeline, not a claim of ownership
of its underlying footage.

## 9. Roadmap

### M2 — style and evaluation (future)

- Build a rights-conscious, opt-in analysis workflow for licensed/public-domain material.
- Produce versioned `STYLE_GUIDE.md` and anonymized timing/mechanism examples.
- Add a fixed-clip A/B harness for Luna, GLM, Qwen, Kimi, and future models.
- Compare prompt variants and causal-profile projection without copying source lines.

### M4 — comedy quality hardening (partial)

- ✅ Dense cadence/audio/cut cue planning.
- ✅ Hot-moment analysis and content-kind registers.
- ✅ Whole-video continuity ledger plus local causal transcript/frame evidence.
- ✅ Judge scores, one-cue rewrites, omitted-cue recovery, and final-manifest truth.
- ✅ Natural reaction delay, intentional overlap, ducking, limiting, and procedural overlay.
- ⏳ Make the continuity profile itself causal per cue.
- ⏳ Add host-segment intros, richer audio-reactive animation, and better callback tracking.

### M5 — operations and voice workflow (future)

- ✅ Add cross-platform bootstrap scripts, dependency manifests, a doctor, and foreground launchers.
- ⏳ Add CI smoke checks on Linux, macOS, and Windows, plus reproducible release artifacts.
- Add provider/model identity to every LLM cache policy and improve shared cache strategy.
- Preserve unchanged TTS artifacts during editor rerenders where safe.
- Consume downloaded subtitles when their timing/quality beats ASR.
- Chunk and merge understanding for feature-length material.
- Add authentication, rate limiting, job quotas, cleanup, and multiple workers.
- Keep the current CLI custom-reference workflow, then add a private WebUI voice library.
  Proposed sequence: (1) upload a short WAV into a private, non-public area; (2) validate
  size, duration, decodability, and channel/sample-rate normalization; (3) require the
  user to confirm they have rights/consent; (4) prepare/cache a PocketTTS state and
  generate a preview; (5) store only a voice ID plus conditioning fingerprint on the
  job; and (6) expose per-job pitch/rate controls. Never expose HF tokens through the
  browser, and never place raw reference audio in public artifacts or repository docs.
- Consider a durable workflow/orchestration layer only if the linear Python pipeline stops
  being sufficient; flue remains an option, not a current dependency.
- Add browser uploads, gallery/community sharing, and richer job inspection only with
  appropriate rights and storage controls.

## 10. Closed architectural decisions

- **Pipeline:** Python-first, deterministic media stages around LLM understand/write/judge.
- **ASR/TTS:** CPU-only Parakeet via sherpa-onnx and PocketTTS; no GPU requirement.
- **Frontend:** minimal static vanilla UI served by FastAPI.
- **Deployment:** local foreground launchers on Linux/macOS/Windows; this VM additionally uses systemd and the exe.dev proxy.
- **Providers:** swappable provider/base URL/model, with Hyper as the default picker entry
  and OpenRouter for broad multimodal selection.
- **Voices/IP:** original personas and procedural silhouettes; built-in/custom voice
  conditioning is allowed only for lawfully usable, consented material. Do not imitate
  named characters, actors, catchphrases, or source dialogue.
- **Timing:** dense cadence is intentional; silence and preferred fit are not hard gates;
  physical media boundaries are the hard limits.
