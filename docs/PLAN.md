# mst3k-anything — Architecture & Plan

**Goal:** paste a YouTube URL → get back a video with witty, MST3K-style robot heckling,
perfectly timed to the action, original audio ducked under the riffs, and a theater/
robot-silhouette overlay along the bottom. All powered by an LLM workflow backed by an
OpenAI-compliant API.

---

## 1. Executive summary

This is buildable, and every risky stage was already **proven end-to-end in this session**
on a 2-core CPU box (see `demo/`):

| Stage | Proven with | Result |
|---|---|---|
| YouTube ingest | yt-dlp 2026.08.19 (`uv tool install`) | 3 min of Plan 9 from Outer Space, 360p mp4 |
| Riff cue detection | ffmpeg + deterministic audio/cut scoring | dense cadence baseline plus silence/visual/audio signals |
| Visual understanding + joke writing | qwen3.8-flash (vision) via Hyper chat-completions API | 3 frame-specific riffs, correct JSON, $0.002 |
| TTS (free, CPU, no GPU) | Pocket TTS (Kyutai) | 3.3s of speech in ~6s on 2 cores; voice cloning available |
| Timing and mix | measured TTS duration + sidechain ducking | preferred landing windows; deliberate dialogue overlap is allowed |
| Voice differentiation | Pocket TTS + ffmpeg pitch shift | 3 distinct characters from pitch-coloring (movie-sign recipe) |
| Audio duck & mix | ffmpeg `volume=between()` + `adelay` + gain | silence window went −45 dB → −18 dB exactly during the riff |
| Theater overlay | stdlib-generated RGBA silhouette PNG | bottom-band brightness 76 → 35, composited for full duration |

The pipeline is: **deterministic media stages (ffmpeg/yt-dlp/TTS) wrapped around two
agentic LLM stages (understand, write)**. That split drives the whole architecture.

---

## 2. Architecture overview

```
┌────────────────────────── FRONTEND (web app) ──────────────────────────┐
│ submit URL · pick options · live job progress · preview player · download │
└───────────────┬───────────────────────────────────────▲────────────────┘
                │ REST/WS submit                        │ status + result
┌───────────────▼────────────── BACKEND ────────────────┴────────────────┐
│ FastAPI + job queue (SQLite-backed) · resumable stage cache            │
├────────────────────────────── PIPELINE ────────────────────────────────┤
│ 1 INGEST      yt-dlp download (+subs if any) → mp4                     │
│ 2 TRANSCRIBE  chunked Parakeet ASR → timestamped transcript            │
│ 3 UNDERSTAND  *LLM AGENT*: metadata + frames + transcript → ledger     │
│ 4 CUE PLAN    cadence + audio + cuts + pauses → dense scored cues      │
│ 5 WRITE       *LLM AGENT*: contextual draft → judge/rewrite             │
│ 6 PLACE       TTS measurement → preferred landing or deliberate overlap │
│ 7 MIX/DELIVER duck + mix → final mp4, SRT, rendered riffs.json         │
└────────────────────────────────────────────────────────────────────────┘
```

**Key design principle:** the LLM never touches media bytes. It consumes manifests
(gaps, timecodes, frames, transcripts) and emits structured JSON (riffs with speaker +
line). All timing-critical work happens in deterministic code that *measures* results.
This is what makes the comedy land in the right places without hallucinated timing.

---

## 3. Pipeline stages in detail

### Stage 1 — Ingest
- `yt-dlp` (installed via `uv tool install yt-dlp`, kept fresh; YouTube breaks old versions).
- Grab best ≤720p mp4 for speed; also pull **auto-captions/subs** when present
  (YouTube auto-caps with timestamps beat transcription for dialogue context).
- Metadata: title, duration, uploader → feeds the "understand" stage.

### Stage 2 — Decompose (all deterministic, cached)
- **Transcript**: Parakeet CTC (INT8, CPU-only) runs in bounded 60-second subprocess
  chunks, cached independently for long-form reliability.
- **Frames**: one anchor frame plus pre/mid/post context frames; downscaled for vision.

### Stage 3 — Understand (LLM agent)
- One vision/text call (or a few chunked calls for long videos): sampled frames plus the
  timestamped transcript → `{premise, characters, tone, running_gags, visual_motifs,
  targets, scene_beats}`. This becomes shared continuity context for the writers' room.
- For feature-length input, chunk by ~10-minute windows with overlap and merge.

### Stage 4 — Cue plan (dense, scored opportunities)
The baseline count comes from the video's content kind plus the density knob. Cadence cues
keep continuous-dialogue, music, and fast-cut material alive; pauses, quietness, scene cuts,
and hot audio moments improve the anchor but are never required. The planner keeps a small
minimum spacing only to deduplicate near-identical cues, not to forbid adjacent comic beats.

### Stage 5 — Write (LLM agent, the heart of the app)
Batched writer calls (batches of ~6–10 cues). Each request carries:
- The evidence-first theater-commentator prompt and a content-kind register.
- The timestamped whole-video transcript, continuity profile, running-gag/motif ledger,
  and local pre/mid/post frames for each cue.
- A preferred word count and landing window, but no hard silence or duration veto.
- Output: strict JSON with one grounded riff object per offered cue, including timing
  intent, comic mechanism, evidence references, and optional callback target.
- A judge sees the complete draft ledger and rewrites salvageable weak jokes before mix.

### Stage 6 — Place (timing is a preference, not a veto)
- TTS each line and measure actual duration with ffprobe.
- Apply only a modest speed-up when it improves a button. If the line is longer than
  its preferred cue, place it anyway over the source dialogue; reject only failed TTS
  or an impossible video-boundary placement.
- The writer's signed timing hint can place a setup before the anchor or a button after it.
- Cache rendered lines keyed by (text, voice) — editor rerenders only re-speak changed lines.

### Stage 7 — Mix and deliver
- Final mp4 (copy video stream when possible, AAC audio), `riffs.srt` for reading along,
  and `riffs.json` containing only the riffs that actually made it into that video.
  Drafts and judge output are kept separately for debugging; editor submissions are
  persisted as a private request manifest and bypass another writer pass.

---

## 4. The LLM layer (models & API)

- Provider: any **OpenAI-compliant chat-completions endpoint** — proven against
  `https://hyper.charm.land/v1/chat/completions` with model `qwen3.8-flash`.
  Provider/base-URL/model all config-driven.
- Per-stage model assignment (config):
  | Stage | Default | Why |
  |---|---|---|
  | Understand | qwen3.8-flash (vision) | cheap, huge context, sees frames |
  | Write | qwen3.8-flash | batch cost pennies per movie; fast |
  | Punch-up (optional) | stronger model | only for top ~20% of gaps |
  | QA judge | same or separately selected model | rewrite weak lines; drop only when no grounded joke remains |
- All responses are schema-validated (pydantic); failures retry with the error appended.
- Prompt cache: STYLE_GUIDE + personas are identical across a job → cache-hit pricing
  ($0.02/M on Hyper) makes long movies even cheaper.

---

## 5. MST3K style framework ("watch MST3K, learn the craft")

Yes — ingesting real MST3K material is the right move, but **ingest to distill, not to
copy**:

1. `yt-dlp` a curated set of official MST3K uploads (the official channel hosts
   clips/episodes; they're also on some free tiers). Personal-use analysis only.
2. For each clip: transcribe with timestamps, separate host/riff lines from movie audio
   (the show's mix makes this tractable: riff lines are clean studio vocal over ducked
   movie audio), and record **where** each riff lands relative to the movie content
   (in a silence? as a button right after a line? over a slow scene? during a song?).
3. Feed transcripts + timings + a few sampled frames to the LLM with an analysis prompt
   → distill: joke typology (observation, anachronism, character-voice, callback,
   sung riff, pun, anti-joke), rhythm/density patterns, how setups/payoffs span gaps,
   what gets riffed (production errors, wooden acting, continuity) and what never does.
4. Output: **`STYLE_GUIDE.md`** + a bank of ~100 anonymized example riffs with their
   timing context. The guide becomes the writer's system prompt; examples become
   few-shot shots matched to gap type.
5. Re-run the distillation whenever we want to tune the voice ("more musical riffs",
   "meaner Crow-analog"), versioning each guide.

This gives the workflow the *framework* the idea calls for — durable, editable, and
detached from any single model.

---

## 6. Workflow / framework choice (incl. the flue question)

**`withastro/flue` assessment** ("The sandbox agent framework", TypeScript):
- Real and on-point for *agentic* work: sessions, durable recovery, skills (markdown
  playbooks), typed tools, sandboxes, subagents, OTel observability. Deploy to Node,
  Cloudflare Workers, GitHub Actions, etc.
- Fit: it's a great home for the **creative agent stages** (understand + write), where
  you want a loop with tools ("get me gap 47's frame", "re-check my budget", "regenerate
  weak riffs") and durable sessions for long jobs.
- Friction: TS-first, while our proven media toolchain is Python (yt-dlp, ffmpeg,
  Pocket TTS, faster-whisper); the repo is young. The agent would shell out to Python/
  ffmpeg tools inside its sandbox — workable, one extra layer.

**Recommendation (pragmatic):**
- **MVP (Phase 1–2): pure-Python pipeline** — one package, stage functions, SQLite job
  table, content-keyed cache at every stage (exactly how the demo scripts already work).
  Simple, boring, and it ships.
- **Phase 3 option: adopt flue** as the orchestration layer for the agent stages once
  we need real autonomy (self-review loops, per-scene subagents, durable retries).
  Because the LLM layer is just an OpenAI-compliant endpoint and the media stages are
  CLI tools, swapping the orchestrator later is cheap.
- Also evaluated and deliberately not used for orchestration: heavyweight workflow
  engines (Temporal etc. — overkill), LangGraph (fine, but we don't need its graph
  abstraction for a near-linear pipeline).

**Reference implementation to mine:** `davidtkunz/movie-sign` (verified real: 1,563
lines, clean code). We validated its writers'-room persona, pydantic riff schema,
word-budget math, and measured-fit mixdown by reading the source. It's Windows-leaning
(SAPI voices) and outputs a *parallel-play* commentary track rather than a muxed video
— our app is the Linux-portable, vision-first, video-muxing superset. We can borrow
structure (and optionally credit it) but we own our pipeline.

---

## 7. Frontend / app

Keep it lean (it's a utility with one hero output):
- **Stack**: FastAPI backend + simple React (Vite) frontend. (htmx is fine if we want
  zero-JS; React if we want a nicer player/progress UX.)
- **Views**: submit (URL + options: riff density, snark level, voice set, duck depth,
  overlay on/off) → job page (SSE progress per stage, live riff preview list) →
  result (player, download mp4/srt, "re-render" after editing riffs.json in-browser).
- Later: gallery of community riffs (requires rights care, §11), side-by-side
  before/after player.

---

## 8. Voices & TTS

- **Engine: Pocket TTS** (Kyutai) — CPU-only (~0.5× realtime on our 2-core box, faster
  on production CPUs), streaming, MIT-ish licensed, free. Runs as library or HTTP
  service (`pocket-tts serve`).
- **Characters**: 3 original bots (avoid trademarked names/voices):
  - Clone 2–3 distinct voices from **public-domain recordings** (Pocket TTS needs ~10 s
    reference clips; sources: old radio dramas / LibriVox on archive.org) — the user
    suggested exactly this and it's the right call.
  - Differentiate further with pitch/coloring (movie-sign recipe): host plain,
    fast-bot −1.5 semitones, theatric-bot +4 semitones.
  - `export-voice` to `.safetensors` once per character → instant loads thereafter.
- Upgrade path (optional, paid): ElevenLabs for "performer-grade" takes; interface
  already abstracted behind one `speak(line, character) -> wav` function.
- Never clone the real MST3K cast/characters; keep original personas (§11).

---

## 9. The timing-precision system (why jokes land "in exactly the right places")

1. Cue opportunities are scored from cadence, scene/visual change, audio energy, quietness,
   and pauses; silence is not a requirement.
2. Writer sees exact local evidence plus whole-video transcript and continuity memory.
3. TTS output is measured for useful timing hints, but overtalk is allowed.
4. Placement is sample-accurate via `adelay`; only video boundaries are hard.
5. Sidechain ducking and a limiter preserve speech intelligibility in dense sections.
6. Judge rewrites weak jokes based on groundedness, comic turn, voice, and intentional timing.

---

## 10. Cost & performance (measured, not guessed)

- Writing a 3-minute sample: ~3k tokens including images ≈ **$0.002**.
- Extrapolated feature (90 min, ~250 riffs, batched): **~$0.05–0.30** in LLM calls,
  dramatically less with prompt caching of the style guide.
- TTS: **$0** (local CPU). Whisper: $0 local. ffmpeg/yt-dlp: $0.
- Encoding: ~16× realtime here → a 90-min movie ≈ 6 min encode on this box.
- Bottleneck is TTS on small CPUs (~30 riffs × ~6 s ≈ 3 min) — parallelize across cores
  or use `pocket-tts serve` with a worker pool.

---

## 11. Legal & IP notes (keep it fan-friendly)

- **Input videos**: users riff content they choose; app is a transformative parody tool,
  but hosting arbitrary YouTube downloads raises ToS/copyright questions — ship with a
  "your responsibility" notice; favor public-domain catalogs for demos (Plan 9, etc.).
- **MST3K itself**: name/characters/brand are protected. App named in homage for
  personal/fan use is one thing; before any public/commercial release, rebrand the
  bots as original characters, use the style guide as *craft learned*, not *lines
  copied*, and don't redistribute MST3K footage. Downloading their videos for private
  style-analysis is a personal-use gray area — fine for research, don't ship it.
- **Voice cloning**: public-domain sources only (already the plan).

---

## 12. Milestones

- **M0 — Proof of concept** ✅ (this session; artifacts in `demo/`).
- **M1 — CLI package** (`mst3k-anything riff <url>`): refactor demo scripts into one
  Python package with stage cache, config file (provider/model/voices/ducking), SRT +
  mp4 out. Exit: one command riffs Plan 9 end-to-end.
- **M2 — Style framework**: MST3K ingest + distillation → STYLE_GUIDE.md v1 + example
  bank; A/B the writer with/without it on the same clips. Voice-cloning pipeline from
  PD sources; three locked character voices.
- **M3 — Service + frontend**: job queue, stage streaming (SSE), submit/player UI,
  riff re-edit + cheap re-render.
- **M4 — Comedy quality loop**: hot-moment ranking, QA judge + rewrite pass, callbacks
  and running-gags across the runtime, host-segment intros, animated MST3K-style
  silhouettes, sidechain ducking.
- **M5 — Ops**: deploy backend+frontend (a single VPS handles everything at this
  scale; GPU optional for whisper-large), rate limiting, optional flue-based agent
  orchestration if we outgrow the linear pipeline.

---

## 13. Open decisions (for you)

1. **Orchestration**: Python-first MVP with flue considered for Phase 3 (recommended),
   or go all-in on flue now?
2. **Characters**: name/vibe for our three original bots (we need stand-ins for the
   host + two robots)?
3. **Frontend flavor**: React+Vite vs htmx-minimal?
4. **Deployment target**: this machine / a VPS / elsewhere?
5. **Hyper as the default provider** (it worked great and is dirt cheap) vs a
   provider-switcher from day one?

---

## 9. Decisions & refinements (session 2)

User decisions:
1. **Voices**: closer-to-MST3K *vibe* but built from public-domain voice samples cloned
   with Pocket TTS + pitch/EQ coloring. Silhouette characters are ORIGINAL (procedural).
2. **Lineup**: solo snarker for v1 (single voice, one riff per window). Trio as v1.x.
3. **Frontend**: htmx/minimal server-rendered.
4. **Deploy**: this VM, systemd service, exe.dev proxy already routes
   `mst3k-anything.exe.xyz` → localhost:8000.
5. **LLM provider**: Hyper default, `.env`-controlled (url/key/model) so it's swappable.

### 9.1 Ingest accepts "anything"

Not just movies — any YouTube video (incl. Shorts), any archive.org direct video-file
URL, other direct video URLs, and (v1.x) browser uploads. All converge on
`source.mp4` + `meta.json` before stage 2, so downstream is source-agnostic.

- YouTube URLs → yt-dlp (formats, subtitles, metadata, age-gates handled).
- archive.org direct links (`*archive.org/download/...mp4|webm|mkv...`) → HTTP
  download with progress + Content-Length.
- archive.org *item* pages (`/details/<id>`) → yt-dlp generic extractor.
- Anything else → probe with HTTP HEAD/`ffprobe`; if it smells like video, download;
  else try yt-dlp generic.
- Guardrails: reject > 2.5 h, corrupt files, no-audio files (riff track is audio).

### 9.2 Content-aware writing (not just bad movies)

Stage 3 (UNDERSTAND) now produces a **content profile**:
`{kind, tone, premise, targets[], visual_gags[], pacing}` from metadata + sampled
frames + transcript. Stage 4 (WRITE) selects its register from the profile:

| kind | riff register |
|---|---|
| bad movie/show | production values, continuity, acting (classic MST3K) |
| vlog/home video | observational teasing about choices, props, editing |
| tutorial | deadpan corrections, over-literal questions |
| gaming/music | play-by-play heckling, beat-synced one-liners |

Anchor strategy: dense cadence baseline + scene cuts, audio hot/quiet moments, and actual
pauses. These signals improve selection and timing intent; they do not veto a strong riff
just because the source is talking.
