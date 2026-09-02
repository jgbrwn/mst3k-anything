"""mst3k-anything CLI: mst3k render <url-or-path> [options]."""
import argparse
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

from . import analyze, config as cfgmod, context, ingest, mix, transcribe, understand, voice, writer


def write_rendered_manifest(job_dir: Path, placements: list[dict]) -> Path:
    """Persist exactly the riffs that mix.build received, in timeline order."""
    manifest = job_dir / "riffs.json"
    rendered = []
    for placement in sorted(placements, key=lambda p: p["start"]):
        item = {"gap": placement["gap"],
                "speaker": placement.get("speaker", "riffer"),
                "line": placement["line"],
                "words": placement.get("words", len(placement["line"].split())),
                "when": placement.get("when", 0.0),
                "start": placement["start"],
                "duration": placement["duration"]}
        for key in ("timing", "mechanism", "evidence", "callback_to", "overlap_allowed"):
            if key in placement:
                item[key] = placement[key]
        rendered.append(item)
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(rendered, indent=2))
    tmp.replace(manifest)
    return manifest


def cmd_prepare_voice(args) -> None:
    """Precompute a PocketTTS conditioning state from a consented reference."""
    job = cfgmod.load()
    source = str(Path(args.source).expanduser()) if "://" not in args.source else args.source
    output = Path(args.out).expanduser().resolve()
    try:
        prepared = voice.prepare_voice_reference(source, output, job["pocket_tts"],
                                                 force=True)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Prepared custom voice: {prepared}")


def cmd_render(args) -> None:
    job = cfgmod.load()
    if args.voice_ref:
        job["voice_ref"] = args.voice_ref
    if args.voice_pitch is not None:
        job["voice_pitch"] = args.voice_pitch
    if args.voice_rate is not None:
        job["voice_rate"] = args.voice_rate
    try:
        voice.validate_voice_reference(job)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    # prefer recent yt-dlp in ~/.local/bin over any system copy; add to PATH
    local_yt = Path.home() / ".local" / "bin"
    if (local_yt / "yt-dlp").exists():
        os.environ["PATH"] = f"{local_yt}{os.pathsep}" + os.environ.get("PATH", "")
    # optional density bias from api (submitted via UI)
    bias = os.environ.get("MST3K_RIFF_DENSITY_BIAS")
    if bias is not None:
        try:
            job["riff_density_bias"] = int(bias)
        except ValueError:
            pass
    job["jobs_dir"].mkdir(parents=True, exist_ok=True)
    url_slug = ingest.slugify(args.source)
    # API jobs get an immutable, per-row work_dir. Direct CLI invocations
    # retain the URL-slug directory as their private workspace.
    requested_dir = os.environ.get("MST3K_JOB_DIR")
    job_dir = Path(requested_dir).resolve() if requested_dir else job["jobs_dir"] / url_slug
    job_dir.mkdir(parents=True, exist_ok=True)
    job["dir"] = job_dir
    artifact_slug = url_slug
    # persist our PID for cancel-from-web killability
    (job_dir / "pid").write_text(str(os.getpid()))
    # start our own process group so os.killpg in cancel hits us + ffmpeg+pockettts
    try:
        os.setsid()
    except OSError:
        pass  # already pg leader

    def step(name, fn):
        t0 = time.time()
        print(f"[{name}] ...", flush=True)
        r = fn()
        print(f"[{name}] done in {time.time()-t0:.1f}s", flush=True)
        return r

    # 1 ingest
    src, meta = step("ingest", lambda: ingest.ingest(args.source, job))
    job["source"], job["meta"] = src, meta
    artifact_slug = ingest.slugify(meta.get("title") or url_slug)
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"    {meta['title'] or 'untitled'} ({meta['duration']:.0f}s, {meta['width']}x{meta['height']})")

    # 2 transcribe before profiling so the analyst and writer get real dialogue
    # context. The ASR stage is chunked and cached, so long videos are bounded.
    asr = step("transcribe", lambda: transcribe.transcribe(job))
    job["transcript"] = asr

    # 3 determine kind after a first frame pass plus transcript. This profile
    # supplies scene/motif/callback guidance to the dense cue planner and writer.
    step("frames", lambda: analyze.grab_frames(job, []))
    profile = step("understand", lambda: understand.build_profile(job))
    job["kind"] = (profile or {}).get("kind", "other")

    # 4 cues: silence is only one signal; cadence guarantees the requested
    # baseline even when a video has continuous dialogue or music.
    gaps = step("gaps", lambda: analyze.find_gaps(job))
    kinds = "+".join(sorted({g["kind"] for g in gaps}))
    print(f"    {len(gaps)} riff cues ({kinds or 'none'}), target={job['target_riff_count']} for kind={job['kind']}")
    if not gaps:
        print("No usable riff cues.")
        sys.exit(0)
    step("frames", lambda: analyze.grab_frames(job, gaps))
    analyze.score_visual_interest(job, gaps)
    hot = analyze.hot_moments(job, job["dir"] / "audio.wav") if meta.get("has_audio", True) else []
    (job_dir / "gaps.json").write_text(json.dumps(gaps, indent=2))
    (job_dir / "hot_moments.json").write_text(json.dumps(hot, indent=2))
    step("context frames", lambda: context.grab_context_frames(job, gaps, hot))

    # 5 bundles now reuse the already completed transcription stage.
    bundles = context.build_bundles(job, gaps, asr, hot)
    (job_dir / "bundles.json").write_text(json.dumps(
        [{k: v for k, v in b.items() if k != "frames"} for b in bundles], indent=1))

    # 5 write: an editor rerender supplies an exact manifest and must bypass
    # the LLM; normal jobs use the writer + judge pipeline.
    requested_path = job_dir / "requested_riffs.json"
    if requested_path.exists():
        print("[write] ...", flush=True)
        try:
            requested = json.loads(requested_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid requested_riffs.json: {exc}")
        gap_by_id = {g["id"]: g for g in gaps}
        riffs = []
        for item in requested:
            if not isinstance(item, dict):
                continue
            try:
                gid = int(item.get("gap"))
            except (TypeError, ValueError):
                continue
            if gid not in gap_by_id:
                continue
            line = str(item.get("line") or "").strip()
            if not line:
                continue
            try:
                when = float(item.get("when", 0.0))
            except (TypeError, ValueError):
                when = 0.0
            timing = str(item.get("timing") or ("overlap" if when < 0 else "cue"))
            if timing not in {"cue", "button", "overlap"}:
                timing = "cue"
            mechanism = str(item.get("mechanism") or "observation")
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            try:
                callback_to = int(item["callback_to"]) if item.get("callback_to") is not None else None
            except (TypeError, ValueError):
                callback_to = None
            requested_start = item.get("start")
            try:
                requested_start = float(requested_start) if requested_start is not None else None
                if requested_start is not None and not math.isfinite(requested_start):
                    requested_start = None
            except (TypeError, ValueError):
                requested_start = None
            riffs.append({"gap": gid, "speaker": str(item.get("speaker") or "riffer"),
                          "line": line, "words": len(line.split()), "when": when,
                          "timing": timing, "mechanism": mechanism,
                          "evidence": evidence[:2], "callback_to": callback_to,
                          "_requested_start": requested_start})
        print(f"[write] done in 0.0s", flush=True)
    else:
        riffs = step("write", lambda: writer.write_riffs_with_review(job, gaps, profile, bundles))
    kept = sum(1 for r in riffs if r.get("_kept_from_rewrite"))
    note = f"{len(riffs)} riffs"
    if kept: note += f" ({kept} improved by judge rewrites)"
    if requested_path.exists():
        print(f"    using editor manifest: {len(riffs)} riffs")
    print(f"    {note}")

    # 6 synthesize + place. The cue envelope is a preferred landing area, not
    # a hard gate: the show may deliberately talk over dialogue.
    def synth_all():
        out = []
        duration = float(job["meta"]["duration"])
        for r in riffs:
            g = next((g for g in gaps if g["id"] == r["gap"]), None)
            if not g:
                continue
            res = voice.synthesize(job, {**r, "_gap": g})
            if not res or not res.get("ok"):
                print(f"    drop gap{r['gap']}: synthesis unavailable")
                continue
            dur = res["duration"]
            try:
                when = float(r.get("when", 0.0))
            except (TypeError, ValueError):
                when = 0.0
            anchor = float(g.get("anchor", g["start"]))
            requested_start = r.get("_requested_start")
            if isinstance(requested_start, (int, float)) and math.isfinite(requested_start):
                start = requested_start
            else:
                reaction = max(0.0, float(job.get("reaction_delay_sec", 0.35)))
                if r.get("timing", "cue") != "overlap":
                    when = max(when, reaction)
                start = anchor + when
            # Only the physical video boundaries are hard. A negative offset
            # is valid setup overlap; a long line may run over dialogue. Do not
            # move a late riff backward merely to preserve its full tail.
            start = max(0.0, start)
            placed_dur = min(dur, max(0.0, duration - start))
            if placed_dur <= 0.0:
                print(f"    drop gap{r['gap']}: no video time remains")
                continue
            headroom = (g["end"] - start) - placed_dur
            out.append({**r, "start": round(start, 3),
                        "wav": res["path"], "duration": placed_dur,
                        "_headroom": round(headroom, 3),
                        "_score": g.get("score", 0),
                        "overlap_allowed": bool(g.get("overlap_allowed", True)),
                        "timing": r.get("timing", "cue"),
                        "mechanism": r.get("mechanism", "observation"),
                        "evidence": r.get("evidence", []),
                        "callback_to": r.get("callback_to")})
        return out

    placements = step("synthesize+place", synth_all)
    print(f"    placed {len(placements)} of {len(riffs)} written; dialogue overlap is allowed")

    # 7 mix (includes theater overlay: animated webm, PNG fallback, or none)
    built = step("mix", lambda: mix.build(job, placements))

    out_dir = Path(args.out).resolve() if args.out else job_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{artifact_slug}_riffed.mp4"
    final_srt = out_dir / f"{artifact_slug}_riffs.srt"
    shutil.copy2(built["video"], final)
    shutil.copy2(built["srt"], final_srt)
    # This is the editor/API source of truth: only riffs that actually made it
    # into the rendered mix, with their final placement/timing.
    write_rendered_manifest(job_dir, placements)
    if requested_path.exists():
        requested_path.unlink()
    print(f"\nDONE: {final}")
    print(f"      {final_srt}")


def main() -> None:
    p = argparse.ArgumentParser(prog="mst3k", description="MST3K-ify any video")
    sub = p.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("prepare-voice", help="precompute a PocketTTS custom voice state")
    pv.add_argument("source", help="consented local WAV/reference audio or .safetensors state")
    pv.add_argument("-o", "--out", required=True,
                    help="output .safetensors path")
    pv.set_defaults(fn=cmd_prepare_voice)

    r = sub.add_parser("render", help="download + riff + render")
    r.add_argument("source", help="YouTube URL, archive.org URL, video URL, or local path")
    r.add_argument("-o", "--out", help="output directory (default: job dir)")
    r.add_argument("--voice-ref", help="local consented WAV or PocketTTS .safetensors voice state")
    r.add_argument("--voice-pitch", type=float,
                    help="global voice pitch offset in semitones")
    r.add_argument("--voice-rate", type=float,
                    help="global voice delivery-rate multiplier")
    r.set_defaults(fn=cmd_render)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
