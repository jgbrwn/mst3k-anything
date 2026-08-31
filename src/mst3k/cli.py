"""mst3k-anything CLI: mst3k render <url-or-path> [options]."""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import analyze, config as cfgmod, context, ingest, mix, transcribe, understand, voice, writer


def write_rendered_manifest(job_dir: Path, placements: list[dict]) -> Path:
    """Persist exactly the riffs that mix.build received, in timeline order."""
    manifest = job_dir / "riffs.json"
    rendered = [{"gap": p["gap"], "speaker": p.get("speaker", "riffer"),
                 "line": p["line"], "words": p.get("words", len(p["line"].split())),
                 "when": p.get("when", 0.0), "start": p["start"],
                 "duration": p["duration"]}
                for p in sorted(placements, key=lambda p: p["start"])]
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(rendered, indent=2))
    tmp.replace(manifest)
    return manifest


def cmd_render(args) -> None:
    job = cfgmod.load()
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

    # 2 determine kind first so density picks the right pace
    # Use understand (sees frames) *and* metadata so we don't guess from a title.
    # Note: understand makes its own LLM call; we cache the profile in job_dir.
    step("frames", lambda: analyze.grab_frames(job, []))
    profile = step("understand", lambda: understand.build_profile(job))
    job["kind"] = (profile or {}).get("kind", "other")

    # 3 gaps (density depends on job["kind"])
    gaps = step("gaps", lambda: analyze.find_gaps(job))
    kinds = "+".join(sorted({g["kind"] for g in gaps}))
    print(f"    {len(gaps)} riff windows ({kinds or 'none'}), target={job['target_riff_count']} for kind={job['kind']}")
    if not gaps:
        print("No usable riff windows.")
        sys.exit(0)
    step("frames", lambda: analyze.grab_frames(job, gaps))
    analyze.score_visual_interest(job, gaps)
    hot = analyze.hot_moments(job, job["dir"] / "audio.wav")
    (job_dir / "gaps.json").write_text(json.dumps(gaps, indent=2))
    (job_dir / "hot_moments.json").write_text(json.dumps(hot, indent=2))
    step("context frames", lambda: context.grab_context_frames(job, gaps, hot))

    # 4 transcribe + bundles
    asr = step("transcribe", lambda: transcribe.transcribe(job))
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
            riffs.append({"gap": gid, "speaker": str(item.get("speaker") or "riffer"),
                          "line": line, "words": len(line.split()), "when": when})
        print(f"[write] done in 0.0s", flush=True)
    else:
        riffs = step("write", lambda: writer.write_riffs_with_review(job, gaps, profile, bundles))
    kept = sum(1 for r in riffs if r.get("_kept_from_rewrite"))
    note = f"{len(riffs)} riffs"
    if kept: note += f" ({kept} improved by judge rewrites)"
    if requested_path.exists():
        print(f"    using editor manifest: {len(riffs)} riffs")
    print(f"    {note}")

    # 5 synthesize + fit
    def synth_all():
        out = []
        for r in riffs:
            g = next((g for g in gaps if g["id"] == r["gap"]), None)
            if not g:
                continue
            res = voice.synthesize(job, {**r, "_gap": g})
            if res and res["ok"]:
                dur = res["duration"]

                # when-hint: writer-specified placement intent
                when = r.get("when", 0.0)
                if isinstance(when, (int, float)) and abs(when) > 0.05:
                    start = g["start"] + max(0.0, when)
                    if start + dur > g["end"] + 0.05:
                        # writer's intent doesn't fit — fall through to mid/gap_start
                        when = 0.0
                if not isinstance(when, (int, float)) or abs(when) <= 0.05:
                    start = (g["start"] + job["margin"]) if g.get("at") == "gap_start" else (
                        (g["start"] + g["end"]) / 2 - dur / 2)
                headroom = (g["end"] - start) - dur
                out.append({**r, "start": round(start, 3),
                            "wav": res["path"], "duration": dur,
                            "_headroom": round(headroom, 3), "_score": g.get("score", 0)})
            else:
                print(f"    drop gap{r['gap']}: doesn't fit")
        return out

    placements = step("synthesize+fit", synth_all)
    fit_note = f"{len(placements)}/{len(riffs)} riff{'s' if len(riffs) > 1 else ''} fit"
    # over-generation: keep the target count, prefer snug fits + better moments
    target = job["target_riff_count"]
    if len(placements) > target:
        ranked = sorted(placements, key=lambda p: (p["_headroom"], -p["_score"]))
        dropped = {p["gap"] for p in ranked[target:]}
        for p in placements:
            if p["gap"] in dropped:
                print(f"    cut gap{p['gap']}: over target")
        placements = [p for p in placements if p["gap"] not in dropped]
    print(f"    placed {len(placements)} of {len(riffs)} written ({fit_note})")

    # 6 mix (includes theater overlay: animated webm, PNG fallback, or none)
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
    r = sub.add_parser("render", help="download + riff + render")
    r.add_argument("source", help="YouTube URL, archive.org URL, video URL, or local path")
    r.add_argument("-o", "--out", help="output directory (default: job dir)")
    r.set_defaults(fn=cmd_render)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
