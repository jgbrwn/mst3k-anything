# mst3k-anything — Architecture & plan

**Status snapshot: September 2, 2026.** This document describes the behavior that is
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
| M5 operations | 🔧 in progress | systemd deployment works on this VM; auth, rate limits, packaging, and scaling remain. |

### Current example

`docs/examples/deadwood-relentless/` contains the latest completed Deadwood run as of
September 2, 2026: OpenRouter `openai/gpt-5.6-luna`, Relentless density (bias `4`),
27 planned cues, 27 rendered riffs, and 8 judge rewrites. The source is about 4:50 at
1280×720. The directory includes the MP4, SRT, final rendered manifest, and a poster.

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

### 3.6 PocketTTS and placement

PocketTTS generates local speech. The default pool is two built-in voices (`alba` and
`jane`) with deterministic per-riff assignment, pitch/rate coloring, and optional
reference-voice configuration. TTS outputs are measured with ffprobe and cached under
`tts/` using text, voice, pitch/rate, reference-file, and delivery-hint inputs.

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

Writer and judge can be assigned separately per submission. Understanding currently
uses the normal resolved writer route; the legacy `LLM_UNDERSTAND_MODEL` setting is not
a separate active role. There is no implemented separate punch-up stage.

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

This is not a controlled benchmark, but across the multimodal models tested so far the
working impression is:

1. **GPT-5.6 Luna** (`openai/gpt-5.6-luna`) — strongest writing and joke landing;
2. **GLM 5.3 Flash** (`z-ai/glm-5.3-flash`) — second-best overall impression, with
   extra structured-output handling needed;
3. **Qwen 3.8 Flash** (`qwen3.8-flash`) and **Kimi-k3-fast** — useful, fast/cheap
   alternatives but generally weaker comic turns and landings in these runs.

Job 62 is the current Luna example: 27/27 planned cues rendered and eight judge
rewrites. These observations can change with prompt/cache state and source material.
A future fixed-clip A/B benchmark should record human ratings, judge scores, rewrite/drop
rates, causal-reference errors, latency, and cost before treating the ordering as a
measurement.

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
tts/, theater.png or theater_anim.webm
final.mp4, riffs.srt, riffs.json
```

Corrupt JSON, zero-duration TTS, stale source/audio/frame policy, and invalid editor
manifests are rejected or regenerated. Known limitations remain: profile/draft policy
markers do not yet fully encode provider/model identity, and editor rerender currently
clears render-dependent TTS/segment directories rather than preserving every unchanged
speech file. Both are backlog items, not guarantees of the current cache behavior.

## 6. Service and frontend

The service is a single FastAPI application served by uvicorn under
`deploy/mst3k-anything.service` on port 8000. The frontend is one static vanilla
HTML/CSS/JavaScript file, not React, Vite, or htmx.

Implemented UI behavior:

- URL submission;
- writer provider/model selection;
- optional separate judge provider/model;
- OpenRouter model discovery and custom model IDs;
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

- ffmpeg, ffprobe, and yt-dlp;
- a Python environment with FastAPI/uvicorn for the service;
- `asr-venv` with sherpa-onnx and numpy;
- `tts-venv` with PocketTTS; and
- `models/parakeet-ctc/model.int8.onnx` plus `tokens.txt`.

The repository is not currently packaged with a console entry point. The direct CLI
form is:

```bash
PYTHONPATH=src python -m mst3k.cli render \
  "https://www.youtube.com/watch?v=VIDEO_ID" --out out/
```

The service receives provider secrets from `.env`/systemd. Per-job provider and model
choices are stored in SQLite and passed to the child process through environment
variables. The VM deployment and exe.dev proxy are working; authentication, rate
limiting, cleanup policy, and horizontal workers remain operational follow-up work.

## 8. Style framework and legal boundary

The current craft framework lives in code prompts and the content-kind register. A
separate `STYLE_GUIDE.md`, few-shot example bank, and automated distillation pipeline
from reference riff material do not exist yet.

If that work is added, it must distill timing and joke mechanisms rather than copy lines,
characters, catchphrases, footage, or voices. The project should keep original personas
and use public-domain or properly licensed reference audio for any future voice cloning.
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

### M5 — operations (future)

- Add installable packaging and reproducible dependency manifests.
- Add provider/model identity to every LLM cache policy and improve shared cache strategy.
- Preserve unchanged TTS artifacts during editor rerenders where safe.
- Consume downloaded subtitles when their timing/quality beats ASR.
- Chunk and merge understanding for feature-length material.
- Add authentication, rate limiting, job quotas, cleanup, and multiple workers.
- Consider a durable workflow/orchestration layer only if the linear Python pipeline stops
  being sufficient; flue remains an option, not a current dependency.
- Add browser uploads, gallery/community sharing, and richer job inspection only with
  appropriate rights and storage controls.

## 10. Closed architectural decisions

- **Pipeline:** Python-first, deterministic media stages around LLM understand/write/judge.
- **ASR/TTS:** CPU-only Parakeet via sherpa-onnx and PocketTTS; no GPU requirement.
- **Frontend:** minimal static vanilla UI served by FastAPI.
- **Deployment:** this VM with systemd and the exe.dev proxy for the current demo.
- **Providers:** swappable provider/base URL/model, with Hyper as the default picker entry
  and OpenRouter for broad multimodal selection.
- **Voices/IP:** original personas and procedural silhouettes; do not imitate named
  characters, actors, catchphrases, or source dialogue.
- **Timing:** dense cadence is intentional; silence and preferred fit are not hard gates;
  physical media boundaries are the hard limits.
