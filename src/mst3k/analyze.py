"""Stage 2: find good riff cues, not just moments of silence.

Silence is useful landing information, but it is only one signal. The real show
also uses quick asides over dialogue, so this module builds a scored cue plan
from cadence, audio texture, scene changes, hot moments, and any pauses that
happen to be available.
"""

import array
import json
import math
import re
import subprocess
import wave
from pathlib import Path

from . import config
from .cache import file_signature


GAP_POLICY_VERSION = 4
AUDIO_WINDOW_VERSION = 1
FRAME_POLICY_VERSION = 1
DENSITY_MULTIPLIERS = (0.55, 0.78, 1.0, 1.4, 1.8)


def extract_audio(job: dict) -> Path:
    out = job["dir"] / "audio.wav"
    if not out.exists():
        subprocess.run([config.tool("ffmpeg"), "-y", "-v", "error", "-i", str(job["source"]),
                        "-vn", "-ac", "1", "-ar", "16000", str(out)], check=True)
    return out


def _seg(job, audio: Path, start: float, end: float) -> Path | None:
    cache = job["dir"] / "segs" / f"{start:.1f}.wav"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        subprocess.run([config.tool("ffmpeg"), "-y", "-v", "error", "-ss", str(start), "-t",
                        str(end - start), "-i", str(audio), "-ac", "1", "-ar", "8000",
                        str(cache)], check=True)
    return cache


def _speech_frac(seg: Path, gate_db: str, min_sil_dur: float, win: float) -> float:
    """Return the fraction above a speech/noise gate for compatibility callers."""
    if win <= 0:
        return 0.0
    proc = subprocess.run([config.tool("ffmpeg"), "-i", str(seg), "-af",
                           f"silencedetect=noise={gate_db}:d={min_sil_dur}",
                           "-f", "null", "-"], capture_output=True, text=True)
    starts, ends = [], []
    for line in proc.stderr.splitlines():
        if "silence_start:" in line:
            starts.append(float(line.split("silence_start:")[1].split()[0]))
        if "silence_end:" in line and "silence_duration:" in line:
            tokens = line.split()
            i = tokens.index("silence_end:")
            ends.append(float(tokens[i + 1]))
    if not starts:
        return 1.0
    if len(ends) < len(starts):
        ends.append(win)
    silent = sum(max(0.0, min(e, win) - max(s, 0.0))
                 for s, e in zip(starts, ends))
    return max(0.0, 1.0 - silent / win)


def _overlaps(gaps, start, end, pad):
    return any(not (end < g["start"] - pad or start > g["end"] + pad) for g in gaps)


def _frange(start, stop, step):
    x = start
    while x <= stop:
        yield x
        x += step


def _audio_duration(audio: Path) -> float:
    try:
        with wave.open(str(audio), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / rate if rate else 0.0
    except (OSError, wave.Error):
        return 0.0


def _effective_lead(job: dict, duration: float) -> float:
    configured = max(0.0, float(job.get("lead_in_sec", 0.0)))
    if duration <= configured + 1.0:
        return min(configured, duration * float(job.get("short_clip_lead_ratio", 0.1)))
    return configured


def _detect_silence(audio: Path, min_gap: float,
                    gate_db: str = "-32dB", min_dur: float = 0.35) -> list[tuple]:
    """Return pauses as a signal, including short and trailing pauses."""
    proc = subprocess.run([config.tool("ffmpeg"), "-i", str(audio), "-af",
                           f"silencedetect=noise={gate_db}:d={min_dur}",
                           "-f", "null", "-"], capture_output=True, text=True)
    starts, ends = [], []
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: ([-\d.]+)", line)
        if m:
            starts.append(float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", line)
        if m:
            ends.append((float(m.group(1)), float(m.group(2))))

    duration = _audio_duration(audio)
    out = []
    end_i = 0
    minimum = min(0.35, max(0.05, float(min_gap or 0.35)))
    for start in starts:
        while end_i < len(ends) and ends[end_i][0] <= start:
            end_i += 1
        if end_i < len(ends):
            end, measured = ends[end_i]
            end_i += 1
        else:
            end, measured = duration, max(0.0, duration - start)
        pause = max(0.0, measured or end - start)
        if pause >= minimum:
            out.append((start, min(end, duration) if duration else end))
    return [(s, e) for s, e in out if e > s]


def _audio_windows(job: dict, audio: Path) -> list[dict]:
    """Compute RMS/peak windows once, without hundreds of ffmpeg processes."""
    win = float(job.get("moment_win_sec", 1.6))
    hop = float(job.get("moment_hop_sec", 1.2))
    signature = {"version": AUDIO_WINDOW_VERSION, "win": win, "hop": hop,
                 "audio": file_signature(audio)}
    cache = job["dir"] / "audio_windows.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            if isinstance(cached, dict) and cached.get("signature") == signature:
                return cached.get("windows", [])
        except (OSError, json.JSONDecodeError):
            pass

    with wave.open(str(audio), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        total_frames = wf.getnframes()
        if rate <= 0 or total_frames <= 0:
            return []
        win_frames = max(1, int(round(win * rate)))
        hop_frames = max(1, int(round(hop * rate)))
        windows = []
        for start_frame in range(0, total_frames, hop_frames):
            end_frame = min(total_frames, start_frame + win_frames)
            if end_frame <= start_frame:
                break
            wf.setpos(start_frame)
            samples = array.array("h")
            samples.frombytes(wf.readframes(end_frame - start_frame))
            if channels > 1:
                samples = samples[::channels]
            if not samples:
                continue
            mean_square = sum(int(x) * int(x) for x in samples) / len(samples)
            rms = math.sqrt(mean_square) / 32768.0
            peak = max(abs(int(x)) for x in samples) / 32768.0
            rms_db = 20.0 * math.log10(max(rms, 1e-5))
            peak_db = 20.0 * math.log10(max(peak, 1e-5))
            windows.append({
                "start": round(start_frame / rate, 3),
                "end": round(end_frame / rate, 3),
                "dur": round((end_frame - start_frame) / rate, 3),
                "quiet_db": round(rms_db, 3),
                "hot_score": round(max(0.0, peak_db - rms_db), 3),
            })

    payload = {"signature": signature, "windows": windows}
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(cache)
    return windows


def _detect_quiet_moments(audio: Path, job: dict) -> list[dict]:
    """Return all audio windows; quietness is not an eligibility gate."""
    duration = float(job["meta"]["duration"])
    lead = _effective_lead(job, duration)
    if duration <= lead:
        return []
    out = []
    for window in _audio_windows(job, audio):
        if window["end"] <= lead:
            continue
        item = dict(window)
        usable = min(max(0.8, window["dur"]), float(job["max_riff_seconds"]))
        item.update({
            "id": 0,
            "start": max(lead, window["start"]),
            "end": min(duration, window["end"]),
            "usable": round(usable, 3),
            "kind": "moment",
            "at": "anchor",
            "budget_words": max(4, int(usable * job["words_per_second"] * 1.25 + 0.5)),
        })
        out.append(item)
    return out


def _rms_db(seg: Path) -> float | None:
    proc = subprocess.run([config.tool("ffmpeg"), "-i", str(seg), "-af", "astats=metadata=0", "-f", "null", "-"],
                          capture_output=True, text=True)
    m = re.search(r"RMS level dB:\s*(-?\d+\.\d+)", proc.stderr)
    return float(m.group(1)) if m else None


def _mk_gap(job, start, end):
    dur = end - start
    if dur <= 0.15:
        return None
    usable = min(float(job["max_riff_seconds"]), max(0.8, dur))
    return {"id": 0, "start": round(start, 3), "end": round(end, 3),
            "anchor": round(start, 3), "dur": round(dur, 3),
            "usable": round(usable, 3), "kind": "silence", "at": "anchor",
            "overlap_allowed": True,
            "budget_words": max(4, int(usable * job["words_per_second"] * 1.25 + 0.5))}


def _spread(anchors, target, gap_sec, duration):
    """Compatibility helper: chronologically select non-overlapping anchors."""
    if not anchors:
        return []
    picked = []
    for anchor in sorted(anchors, key=lambda a: a["start"]):
        if len(picked) >= target:
            break
        if any(not (anchor["end"] <= p["start"] or anchor["start"] >= p["end"])
               for p in picked):
            continue
        if picked and anchor["start"] - picked[-1]["start"] < gap_sec:
            continue
        picked.append(anchor)
    return sorted(picked, key=lambda g: g["start"])


def _density_multiplier(job: dict) -> float:
    try:
        index = int(job.get("riff_density_bias", 2))
    except (TypeError, ValueError):
        index = 2
    return DENSITY_MULTIPLIERS[max(0, min(len(DENSITY_MULTIPLIERS) - 1, index))]


def target_riff_count(job: dict, duration: float | None = None) -> int:
    """Return the desired writing count; it is not a post-fit placement cap."""
    duration = float(duration if duration is not None else job["meta"]["duration"])
    kind = job.get("kind", "other")
    pace = (job.get("riff_pace_per_kind") or {}).get(
        kind, {"lo": 16.0, "hi": 28.0})
    midpoint = max(1.0, (float(pace["lo"]) + float(pace["hi"])) / 2.0)
    target = max(1, int(round(duration / midpoint * _density_multiplier(job))))
    target = min(target, int(job.get("max_riffs", 400)))
    if job.get("target_riff_count") is not None:
        try:
            target = min(target, max(1, int(job["target_riff_count"])))
        except (TypeError, ValueError):
            pass
    return target


def _cue(job: dict, anchor: float, source: str, score: float,
         preferred_start: float | None = None,
         preferred_end: float | None = None) -> dict:
    duration = float(job["meta"]["duration"])
    lead = _effective_lead(job, duration)
    if duration <= lead:
        anchor = max(0.0, duration / 2.0)
    else:
        anchor = max(lead, min(duration - 0.2, float(anchor)))
    if preferred_start is None or preferred_end is None:
        start = max(lead, anchor - 0.8)
        end = min(duration, anchor + 2.0)
    else:
        start = max(lead, min(duration, float(preferred_start)))
        end = min(duration, max(start + 0.2, float(preferred_end)))
    if end <= start:
        start = max(0.0, min(anchor, max(0.0, duration - 0.8)))
        end = min(duration, start + 0.8)
    usable = min(float(job["max_riff_seconds"]), max(0.8, end - start))
    return {
        "id": 0,
        "start": round(start, 3),
        "end": round(end, 3),
        "anchor": round(anchor, 3),
        "dur": round(end - start, 3),
        "usable": round(usable, 3),
        "preferred_start": round(start, 3),
        "preferred_end": round(end, 3),
        "kind": source,
        "at": "anchor",
        "overlap_allowed": True,
        "candidate_score": round(float(score), 3),
        "budget_words": max(4, int(usable * job["words_per_second"] * 1.25 + 0.5)),
    }


def _policy_signature(job: dict) -> dict:
    kind = job.get("kind", "other")
    duration = float(job["meta"]["duration"])
    return {
        "version": GAP_POLICY_VERSION,
        "kind": kind,
        "bias": int(job.get("riff_density_bias", 2)),
        "pace": (job.get("riff_pace_per_kind") or {}).get(kind),
        "max_riffs": int(job.get("max_riffs", 400)),
        "lead_in_sec": float(job.get("lead_in_sec", 0.0)),
        "short_clip_lead_ratio": float(job.get("short_clip_lead_ratio", 0.1)),
        "min_riff_space_sec": float(job.get("min_riff_space_sec", 2.5)),
        "moment_win_sec": float(job.get("moment_win_sec", 1.6)),
        "moment_hop_sec": float(job.get("moment_hop_sec", 1.2)),
        "silence_gate_db": job.get("silence_gate_db", "-25dB"),
        "silence_min_dur": float(job.get("silence_min_dur", 0.35)),
        "duration": round(duration, 2),
        "source": file_signature(job.get("source", "")),
        "audio": (file_signature(job["dir"] / "audio.wav")
                  if job.get("meta", {}).get("has_audio", True) else None),
        "explicit_cap": job.get("target_riff_count"),
    }


def _select_cues(candidates: list[dict], cadence: list[dict], target: int,
                 min_space: float) -> list[dict]:
    """Keep uniform coverage, replacing slots with stronger contextual signals."""
    selected = list(cadence[:target])
    if len(selected) < target:
        selected.extend(candidates[:target - len(selected)])
    selected = selected[:target]
    others = sorted((c for c in candidates if c not in selected),
                    key=lambda c: (-c.get("candidate_score", 0), c["anchor"]))
    for candidate in others:
        if not selected:
            break
        slot_i = min(range(len(selected)),
                     key=lambda i: abs(selected[i]["anchor"] - candidate["anchor"]))
        if candidate["candidate_score"] <= selected[slot_i].get("candidate_score", 0) + 0.04:
            continue
        if any(i != slot_i and
               abs(selected[i]["anchor"] - candidate["anchor"]) < min_space
               for i in range(len(selected))):
            continue
        selected[slot_i] = candidate
    return sorted(selected, key=lambda c: c["anchor"])


def find_gaps(job: dict) -> list[dict]:
    """Build a dense, scored riff-cue plan."""
    cache = job["dir"] / "gaps.json"
    policy_path = job["dir"] / "gaps_policy.json"
    signature = _policy_signature(job)
    if cache.exists() and policy_path.exists():
        try:
            if json.loads(policy_path.read_text()) == signature:
                gaps = json.loads(cache.read_text())
                job["target_riff_count"] = target_riff_count(job)
                return gaps
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    has_audio = job.get("meta", {}).get("has_audio", True)
    audio = extract_audio(job) if has_audio else None
    signature = _policy_signature(job)
    duration = float(job["meta"]["duration"])
    target = target_riff_count(job, duration)
    job["target_riff_count"] = target
    lead = _effective_lead(job, duration)
    floor = min(max(lead, 0.5), max(0.5, duration - 0.8))
    ceiling = max(floor + 0.2, duration - 0.25)
    span = max(0.2, ceiling - floor)

    cadence = []
    for index in range(target):
        anchor = floor + span * (index + 0.5) / target
        cadence.append(_cue(job, anchor, "cadence", 0.38))
    candidates = list(cadence)

    if audio is not None:
        sil = _detect_silence(audio, job.get("min_gap", 0.35),
                              gate_db=job.get("silence_gate_db", "-25dB"),
                              min_dur=job.get("silence_min_dur", 0.35))
        for start, end in sil:
            candidates.append(_cue(
                job, start + min(0.25, (end - start) / 3.0), "silence", 0.98,
                preferred_start=start, preferred_end=end))

        moments = _detect_quiet_moments(audio, job)
        quiet = sorted(moments, key=lambda m: m.get("quiet_db", 0.0))
        limit = max(12, target * 2)
        for rank, moment in enumerate(quiet[:limit]):
            score = 0.58 + 0.34 * (1.0 - rank / max(1, len(quiet[:limit]) - 1))
            candidates.append(_cue(
                job, (moment["start"] + moment["end"]) / 2, "quiet", score,
                moment["start"], moment["end"]))

        windows = _audio_windows(job, audio)
        hot_count = max(8, min(30, target // 4 or 1))
        for rank, window in enumerate(
                sorted(windows, key=lambda w: -w.get("hot_score", 0))[:hot_count]):
            score = 0.82 + 0.28 * (1.0 - rank / max(1, hot_count - 1))
            candidates.append(_cue(
                job, (window["start"] + window["end"]) / 2, "hot", score,
                window["start"], window["end"]))

    try:
        cuts = find_cuts(job, max_cuts=min(400, max(30, target * 3)))
    except subprocess.CalledProcessError:
        cuts = []
    for cut in cuts:
        if lead <= cut < duration - 0.25:
            candidates.append(_cue(job, cut, "cut", 0.76, cut - 0.7, cut + 1.8))

    selected = _select_cues(candidates, cadence, target,
                            max(1.5, float(job.get("min_riff_space_sec", 2.5))))
    if not selected and duration > 0:
        selected = [_cue(job, min(max(lead, 0.2), max(0.2, duration - 0.2)),
                        "cadence", 0.38)]
    for index, cue in enumerate(selected, 1):
        cue["id"] = index
    cache.write_text(json.dumps(selected, indent=2))
    policy_path.write_text(json.dumps(signature, indent=2))
    return selected


def _astats(seg: Path):
    proc = subprocess.run([config.tool("ffmpeg"), "-i", str(seg), "-af",
                           "astats=metadata=0", "-f", "null", "-"],
                          capture_output=True, text=True)
    m1, m2 = re.findall(r"RMS level dB:\s*(-?\d+\.\d+)", proc.stderr), \
             re.findall(r"Peak level dB:\s*(-?\d+\.\d+)", proc.stderr)
    if m1 and m2:
        return float(m1[-1]), float(m2[-1])
    return None


def hot_moments(job: dict, audio: Path, k: int = 8) -> list[float]:
    """Return high-arousal timestamps from the shared audio-window cache."""
    if not job.get("meta", {}).get("has_audio", True) or not audio.exists():
        return []
    windows = _audio_windows(job, audio)
    return sorted(round((window["start"] + window["end"]) / 2, 1)
                   for window in sorted(windows, key=lambda w: -w.get("hot_score", 0))[:k])


def find_cuts(job: dict, max_cuts: int = 400) -> list[float]:
    """Scene-change timestamps, cached against the source fingerprint."""
    cache = job["dir"] / "cuts.json"
    marker = job["dir"] / "cuts_policy.json"
    signature = {"version": GAP_POLICY_VERSION,
                 "source": file_signature(job.get("source", "")),
                 "max_cuts": max_cuts}
    if cache.exists() and marker.exists():
        try:
            if json.loads(marker.read_text()) == signature:
                return json.loads(cache.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    proc = subprocess.run([config.tool("ffmpeg"), "-i", str(job["source"]), "-vf",
                           "select='gt(scene,0.4)',showinfo", "-f", "null", "-"],
                          capture_output=True, text=True)
    cuts = []
    for line in proc.stderr.splitlines():
        match = re.search(r"pts_time:([\d.]+)", line)
        if match:
            cuts.append(round(float(match.group(1)), 3))
            if len(cuts) >= max_cuts:
                break
    cache.write_text(json.dumps(cuts))
    marker.write_text(json.dumps(signature, indent=2))
    return cuts


def score_visual_interest(job: dict, gaps: list[dict]) -> None:
    """Per-cue 0..1 visual-interest score from frame luminance variation."""
    raw = [_luma_variance(job["dir"] / "frames" / f"gap{g['id']:03d}.png")
           for g in gaps]
    mx = max(raw) if raw else 1.0
    mx = mx if mx > 0 else 1.0
    for gap, value in zip(gaps, raw):
        gap["score"] = round((value or 0) / mx, 3)


def _luma_variance(path: Path) -> float:
    """Return grayscale standard deviation from ffmpeg showinfo."""
    if not path.exists():
        return 0.0
    proc = subprocess.run([config.tool("ffmpeg"), "-v", "info", "-i", str(path), "-vf",
                           "scale=64:36,format=gray,showinfo", "-frames:v", "1",
                           "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"stdev:\[([\d.]+)", proc.stderr)
    return float(m.group(1)) if m else 0.0


def _frame_policy(job: dict, gaps: list[dict]) -> dict:
    return {"version": FRAME_POLICY_VERSION,
            "source": file_signature(job.get("source", "")),
            "cues": [{"id": g["id"], "anchor": round(float(g.get("anchor", 0)), 3),
                      "start": round(float(g["start"]), 3),
                      "end": round(float(g["end"]), 3)} for g in gaps]}


def grab_frames(job: dict, gaps: list[dict]) -> None:
    """Capture an anchor frame plus profile frames."""
    frames = job["dir"] / "frames"
    frames.mkdir(exist_ok=True)
    dur = job["meta"]["duration"]
    marker = job["dir"] / "frames_policy.json"
    policy = _frame_policy(job, gaps)
    old_policy = None
    if marker.exists():
        try:
            old_policy = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    if old_policy != policy:
        for old_frame in frames.glob("gap*.png"):
            old_frame.unlink(missing_ok=True)
        if old_policy and old_policy.get("source") != policy.get("source"):
            for old_frame in frames.glob("ctx*.png"):
                old_frame.unlink(missing_ok=True)
    for gap in gaps:
        t = gap.get("anchor", (gap["start"] + gap["end"]) / 2)
        frame = frames / f"gap{gap['id']:03d}.png"
        if not frame.exists():
            subprocess.run([config.tool("ffmpeg"), "-y", "-v", "error", "-ss", str(t),
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(frame)])
    for i in range(10):
        t = dur * (i + 0.5) / 10
        frame = frames / f"ctx{i:02d}.png"
        if not frame.exists():
            subprocess.run([config.tool("ffmpeg"), "-y", "-v", "error", "-ss", str(t),
                            "-i", str(job["source"]), "-frames:v", "1",
                            "-vf", f"scale={job['frame_width']}:-1", str(frame)])
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(policy, indent=2))
    tmp.replace(marker)
