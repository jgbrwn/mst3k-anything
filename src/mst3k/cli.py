"""mst3k-anything CLI: mst3k render <url-or-path> [options]."""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import analyze, config as cfgmod, ingest, mix, theater, understand, voice, writer


def cmd_render(args) -> None:
    job = cfgmod.load()
    job["jobs_dir"].mkdir(parents=True, exist_ok=True)
    slug = ingest.slugify(args.source)
    job_dir = job["jobs_dir"] / slug
    job_dir.mkdir(exist_ok=True)
    job["dir"] = job_dir

    def step(name, fn):
        t0 = time.time()
        print(f"[{name}] ...", flush=True)
        r = fn()
        print(f"[{name}] done in {time.time()-t0:.1f}s", flush=True)
        return r

    # 1 ingest
    src, meta = step("ingest", lambda: ingest.ingest(args.source, job))
    job["source"], job["meta"] = src, meta
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"    {meta['title'] or 'untitled'} ({meta['duration']:.0f}s, {meta['width']}x{meta['height']})")

    # 2 analyze
    gaps = step("gaps", lambda: analyze.find_gaps(job))
    kinds = "+".join(sorted({g["kind"] for g in gaps}))
    print(f"    {len(gaps)} riff windows ({kinds or 'none'})")
    if not gaps:
        print("No usable riff windows (neither silence nor a quiet moment).")
        sys.exit(0)
    step("frames", lambda: analyze.grab_frames(job, gaps))

    # 3 understand
    profile = step("understand", lambda: understand.build_profile(job))
    print(f"    kind={profile.get('kind')}")

    # 4 write
    riffs = step("write", lambda: writer.write_riffs(job, gaps, profile))
    print(f"    {len(riffs)} riffs written")

    # 5 synthesize + fit
    def synth_all():
        out = []
        for r in riffs:
            g = next((g for g in gaps if g["id"] == r["gap"]), None)
            if not g:
                continue
            res = voice.synthesize(job, {**r, "_gap": g})
            if res and res["ok"]:
                start = (g["start"] + job["margin"]) if g.get("at") == "gap_start" else (
                    (g["start"] + g["end"]) / 2 - res["duration"] / 2)
                out.append({**r, "start": round(start, 3),
                            "wav": res["path"], "duration": res["duration"]})
            else:
                print(f"    drop gap{r['gap']}: doesn't fit")
        return out
    placements = step("synthesize+fit", synth_all)

    # 6 theater overlay + mix
    step("overlay", lambda: theater.make_theater(job_dir / "theater.png", meta["width"]))
    built = step("mix", lambda: mix.build(job, placements))

    out_dir = Path(args.out).resolve() if args.out else job_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{slug}_riffed.mp4"
    shutil.copy2(built["video"], final)
    shutil.copy2(built["srt"], out_dir / f"{slug}_riffs.srt")
    print(f"\nDONE: {final}")
    print(f"      {out_dir / (slug + '_riffs.srt')}")


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
