"""Stage 2: decompose — audio-true riff windows, scene cuts, frames."""

import json
import re
import subprocess
from pathlib import Path




def extract_audio(job: dict) -> Path:
    out = job["dir"] / "audio.wav"
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(job["source"]),
                        "-vn", "-ac", "1", "-ar", "16000", str(out)], check=True)
    return out



def _seg(job, audio: Path, start: float, end: float) -> Path | None:
    cache = job["dir"] / "segs" / f"{start:.1f}.wav"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t",
                        str(end - start), "-i", str(audio), "-ac", "1", "-ar", "8000",
                        str(cache)], check=True)
    return cache


def _speech_frac(seg: Path, gate_db: str, min_sil_dur: float, win: float) -> float:
    """Fraction of the window that is above `gate_db` (speech intensity),
    measured via silencedetect + silence-fraction. Robust vs the old binary
    check that falsely cleared dialogue windows when the gate was too strict."""
    proc = subprocess.run(["ffmpeg", "-i", str(seg), "-af",
                           f"silencedetect=noise={gate_db}:d={min_sil_dur}",
                           "-f", "null", "-"], capture_output=True, text=True)
    starts, ends = [], []
    for l in proc.stderr.splitlines():
        if "silence_start:" in l:
            starts.append(float(l.split("silence_start:")[1].split()[0]))
        if "silence_end:" in l and "silence_duration:" in l:
            toks = l.split()
            i = toks.index("silence_end:")
            ends.append(float(toks[i + 1]))
    if not starts:
        return 1.0
    if len(ends) < len(starts):
        ends.append(win)
    sil = 0.0
    for s, e in zip(starts, ends):
        sil += max(0.0, min(e, win) - max(s, 0.0))
    return max(0.0, 1.0 - (sil / win))


def _overlaps(gaps, start, end, pad):
    return any(not (end < g["start"] - pad or start > g["end"] + pad) for g in gaps)


def _frange(start, stop, step):
    x = start
    while x <= stop:
        yield x
        x += step


def _detect_silence(audio: Path, min_gap: float,
                    gate_db: str = "-32dB", min_dur: float = 0.7) -> list[tuple]:
    proc = subprocess.run(["ffmpeg", "-i", str(audio), "-af",
                           f"silencedetect=noise={gate_db}:d={min_dur}",
                           "-f", "null", "-"], capture_output=True, text=True)
    starts, ends = [], []
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: ([-\d.]+)", line)
        if m: starts.append(float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", line)
        if m: ends.append((float(m.group(1)), float(m.group(2))))
    out = []
    for i, (end, dur) in enumerate(ends):
        s = starts[i] if i < len(starts) else end - dur
        if dur >= min_gap:
            out.append((s, end))
    return out


def _detect_quiet_moments(audio: Path, job: dict) -> list[dict]:
    """Rank windows by RMS quietness. We no longer reject by speech-frac
    outright: on tightly narrated videos every moment is 100% speech, so
    gating on that discards everything. Sidechain ducking handles the
    undertalk on playback — instead pick the quietest viable windows."""
    duration = job["meta"]["duration"]
    win, hop = job["moment_win_sec"], job["moment_hop_sec"]
    lead = job.get("lead_in_sec", 0.5)
    out = []
    for start in _frange(lead, max(lead, duration - win), hop):
        end = min(start + win, duration)
        aseg = _seg(job, audio, start, end)
        if aseg is None: continue
        rms = _rms_db(aseg)
        if rms is None: continue
        usable = min(end - start - 0.4, job["max_riff_seconds"])
        if usable < 0.5: continue
        out.append({"id":0, "start":round(start,1), "end":round(end,1),
                    "dur":round(end-start,1), "usable":round(usable,1),
                    "kind":"moment", "at":"mid", "quiet_db":rms,
                    "budget_words":max(3,int(usable * job["words_per_second"] + 0.5))})
    out.sort(key=lambda x: x["quiet_db"])
    # take the quietest quartile — runtime-scaled so we don't drown in candidates
    keep_n = max(4, len(out) // 4)
    return out[:keep_n]


def _rms_db(seg: Path) -> float | None:
    proc = subprocess.run(["ffmpeg","-i",str(seg),"-af","astats=metadata=0","-f","null","-"],
                          capture_output=True,text=True)
    m = re.search(r"RMS level dB:\s*(-?\d+\.\d+)", proc.stderr)
    return float(m.group(1)) if m else None


def _mk_gap(job, start, end):
    dur = end - start
    if dur < job["min_gap"]: return None
    usable = max(0.4, min(dur - 2 * job["margin"], job["max_riff_seconds"]))
    return {"id": 0, "start": round(start,3), "end": round(end,3),
            "dur": round(dur,3), "usable": round(usable,3), "kind":"silence",
            "at":"gap_start",
            "budget_words": max(3,int(usable * job["words_per_second"] + 0.5))}


def _spread(anchors, target, gap_sec, duration):
    """Pick silence anchors chronologically, no overlap, spaced >= gap_sec."""
    if not anchors: return []
    picked = []
    for a in sorted(anchors, key=lambda a: a["start"]):
        if len(picked) >= target: break
        if any(not (a["end"] <= p["start"] or a["start"] >= p["end"]) for p in picked):
            continue
        if picked and (a["start"] - picked[-1]["start"]) < gap_sec:
            continue
        picked.append(a)
    picked.sort(key=lambda g: g["start"])
    return picked


def _fill_with_moments(audio, job, gaps, target, gap_sec, duration):
    """Top up picks with moments ranked by RMS quietness. The frac gate is
    dropped — refer to _detect_quiet_moments docstring; on narrated videos
    every moment exceeds it."""
    if len(gaps) >= target:
        return gaps
    final = _detect_quiet_moments(audio, job)
    picked_set = {(g["start"], g["end"]) for g in gaps}
    out = list(gaps)
    for m in final:
        if len(out) >= target:
            break
        if (m["start"], m["end"]) in picked_set:
            continue
        if any(not (m["end"] <= p["start"] or m["start"] >= p["end"]) for p in out):
            continue
        out.append(m)
        out.sort(key=lambda g: g["start"])
    return out


def find_gaps(job: dict) -> list[dict]:
    cache = job["dir"] / "gaps.json"
    if cache.exists():
        return json.loads(cache.read_text())
    audio = extract_audio(job)
    duration = job["meta"]["duration"]

    sil = _detect_silence(audio, job["min_gap"],
                          gate_db=job.get("silence_gate_db","-25dB"),
                          min_dur=job.get("silence_min_dur",0.5))
    sil_gaps = [g for g in (_mk_gap(job,s,e) for s,e in sil) if g]

    pace = (job.get("riff_pace_per_kind") or {}).get(
        job.get("kind","other"), {"lo":30.0,"hi":70.0})
    # lo/hi are SECONDS between riffs. Lower = denser. The target number of
    # riffs is duration / midpoint pace (binary-search sense: average).
    midpoint = (pace["lo"] + pace["hi"]) / 2
    ideal  = max(1, int(duration / midpoint))
    ceiling= max(2, int(duration / pace["hi"]))
    target = max(1, int(min(max(len(sil_gaps), ceiling), ideal)))
    if "target_riff_count" in job:  # .env safety cap
        target = min(target, int(job["target_riff_count"]))
    job["target_riff_count"] = target
    pool = max(1, int(target * job.get("window_pool_factor",1.6)))
    gap_sec = max(duration/max(pool,1), job["min_riff_space_sec"])

    gaps = _spread(sil_gaps, target, gap_sec, duration)

    # Fill uncovered stretches: we want at least a couple of windows per half-
    # video. If coverage is thin, pull in quiet moments ranked by how much
    # they synthesize the largest uncovered stretch.
    def span_max(sel):
        ts=[0.0]+[g["start"] for g in sel]+[duration]
        return max(ts[i+1]-ts[i] for i in range(len(ts)-1))

    while len(gaps) < target:
        moments = _detect_quiet_moments(audio, job)
        uses = {(g["start"], g["end"]) for g in gaps}
        best = None; best_score = (float('inf'), float('inf'))
        cur = span_max(gaps)
        for m in moments:
            if (m["start"], m["end"]) in uses:
                continue
            if any(not (m["end"] <= g["start"] or m["start"] >= g["end"]) for g in gaps):
                continue
            hyp = gaps + [m]; hyp.sort(key=lambda g: g["start"])
            s = span_max(hyp)
            # smaller span wins; ties broken by RMS quietness
            score = (s, m["quiet_db"])
            if score < best_score:
                best_score, best = score, m
        if not best or best_score[0] >= cur:
            break
        gaps.append(best); gaps.sort(key=lambda g: g["start"])

    for i,g in enumerate(gaps,1): g["id"]=i
    cache.write_text(json.dumps(gaps,indent=2))
    return gaps


def _astats(seg: Path):
    proc = subprocess.run(["ffmpeg", "-i", str(seg), "-af",
                           "astats=metadata=0", "-f", "null", "-"],
                          capture_output=True, text=True)
    m1, m2 = re.findall(r"RMS level dB:\s*(-?\d+\.\d+)", proc.stderr), \
             re.findall(r"Peak level dB:\s*(-?\d+\.\d+)", proc.stderr)
    if m1 and m2:
        return float(m1[-1]), float(m2[-1])
    return None


def hot_moments(job: dict, audio: Path, k: int = 5) -> list[float]:
    """Peak-arousal timestamps (budget-capped) so the writer treats these as
    high-value riff anchors regardless of their quiet score."""
    duration = job["meta"]["duration"]
    win, hop = 1.6, 1.6
    scored = []
    for start in _frange(0.5, max(0.5, duration - win), hop):
        end = min(start + win, duration)
        aseg = _seg(job, audio, start, end)
        if aseg is None:
            continue
        info = _astats(aseg)
        if not info:
            continue
        mean_db, peak_db = info
        arousal = peak_db - mean_db  # big transient = something happened
        scored.append((arousal, start))
    scored.sort(key=lambda x: -x[0])
    return sorted(round(s, 1) for _a, s in scored[:k])


def find_cuts(job: dict, max_cuts: int = 400) -> list[float]:
    """Scene-change timestamps, cached."""
    cache = job["dir"] / "cuts.json"
    if cache.exists():
        return json.loads(cache.read_text())
    proc = subprocess.run(["ffmpeg", "-i", str(job["source"]), "-vf",
                           "select='gt(scene,0.4)',showinfo", "-f", "null", "-"],
                          capture_output=True, text=True)
    cuts = []
    for line in proc.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            cuts.append(round(float(m.group(1)), 3))
            if len(cuts) >= max_cuts:
                break
    cache.write_text(json.dumps(cuts))
    return cuts


def score_visual_interest(job: dict, gaps: list[dict]) -> None:
    """Per-gap 0..1 visual-interest score (luma-variance proxy for busy-ness).
    Lets the writer prioritize busy moments over static shots."""
    raw = [_luma_variance(job["dir"] / "frames" / f"gap{g['id']:03d}.png")
           for g in gaps]
    mx = max(raw) if raw else 1.0
    mx = mx if mx > 0 else 1.0
    for g, v in zip(gaps, raw):
        g["score"] = round((v or 0) / mx, 3)


def _luma_variance(path: Path) -> float:
    """Busier shots have higher luma variance at low res. 0 if unreadable."""
    if not path.exists():
        return 0.0
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                           "scale=64:36,format=gray,signalstats",
                           "-f", "null", "-"],
                          capture_output=True, text=True)
    m = re.search(r"YAVG:([\d.]+)", proc.stderr)
    return float(m.group(1)) if m else 0.0


def grab_frames(job: dict, gaps: list[dict]) -> None:
    """One keyframe at the middle of each gap + ~10 context frames for the
    understand stage."""
    frames = job["dir"] / "frames"
    frames.mkdir(exist_ok=True)
    dur = job["meta"]["duration"]
    for g in gaps:
        t = (g["start"] + g["end"]) / 2
        f = frames / f"gap{g['id']:03d}.png"
        if not f.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(f)])
    n = 10
    for i in range(n):
        t = dur * (i + 0.5) / n
        f = frames / f"ctx{i:02d}.png"
        if not f.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(f)])
