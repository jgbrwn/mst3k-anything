"""Stage 6: duck-and-mix original audio + riff track + theater overlay; emit SRT."""
import json
import subprocess

from . import config
from .analyze import grab_frames  # noqa: F401  (re-export convenience)


def build(job: dict, placements: list[dict]) -> dict:
    """placements: [{start, wav, duration, gap_id, line}]. Returns paths."""
    out = job["dir"] / "final.mp4"
    srt = job["dir"] / "riffs.srt"

    # theater overlay: animated webm if enabled, else static PNG, else none
    from .theater import make_animated_theater, make_theater
    png = job["dir"] / "theater.png"
    src_w = job["frame_width"]
    overlay_src = None
    if job.get("animated_overlay"):
        anim = job["dir"] / "theater_anim.webm"
        if make_animated_theater(job, anim, frames=16, width=src_w).exists():
            overlay_src = anim
    if overlay_src is None:
        # static fallback (fast, single-frame PNG)
        if not png.exists():
            make_theater(png, src_w)
        overlay_src = png

    # --- SRT of the actual riff audio spans (read-along / verify artifact) ---
    duration = max(0.0, float(job["meta"].get("duration", 0.0)))
    def ts(s):
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")
    with open(srt, "w") as f:
        for i, p in enumerate(sorted(placements, key=lambda p: p["start"]), 1):
            start = max(0.0, float(p["start"]))
            end = min(duration, start + max(0.0, float(p["duration"])))
            if end <= start:
                continue
            f.write(f"{i}\n{ts(start)} --> {ts(end)}\n"
                    f"{p['line']}\n\n")

    if not placements:
        # no riffs survived: still ship the overlaid video
        cmd = [config.tool("ffmpeg"), "-y", "-v", "error", "-i", str(job["source"])]
        if overlay_src:
            # Both overlay variants must be bounded by the source duration.
            # stream_loop=-1 without -shortest makes a static PNG render forever.
            if overlay_src.suffix == ".webm":
                cmd += ["-stream_loop", "-1"]
            else:
                cmd += ["-loop", "1"]
            overlay_eof = ("shortest=1:eof_action=endall"
                           if overlay_src.suffix == ".webm" else "eof_action=repeat")
            cmd += ["-i", str(overlay_src), "-filter_complex",
                    f"[0:v][1:v]overlay=x=(W-w)/2:y=H-h:{overlay_eof}[vout]",
                    "-map", "[vout]", "-map", "0:a?", "-shortest"]
        else:
            cmd += ["-c", "copy"]
        cmd.append(str(out))
        subprocess.run(cmd, check=True)
        return {"video": out, "srt": srt}

    # --- audio graph: duck original under each riff, riffs louder ---
    theater = job["dir"] / "theater.png"
    has_theater = theater.exists()

    inputs = ["-i", str(job["source"])]
    source_audio_idx = 0
    riff_base_idx = 1
    if not job.get("meta", {}).get("has_audio", True):
        # A video-only submission still gets a valid riff audio bus.
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        source_audio_idx = 1
        riff_base_idx = 2
    for p in placements:
        inputs += ["-i", str(p["wav"])]
    tidx = None
    if overlay_src:
        tidx = riff_base_idx + len(placements)
        if overlay_src.suffix == ".webm":
            inputs += ["-stream_loop", "-1"]
        inputs += ["-i", str(overlay_src)]

    parts = []
    for i, p in enumerate(placements, start=0):
        delay_ms = int(p["start"] * 1000)
        # apad + atrim forces the riff bus to span the whole timeline so amix
        # doesn't treat EOF as a dropout, and the sidechaincompress sidechain
        # always has input after the riff ends (fixes post-riff silence).
        parts.append(
            f"[{riff_base_idx + i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms},apad,atrim=0:{duration:.3f},"
            f"volume={job['riff_gain']:.2f}[r{i + 1}]")
    # sidechain ducking: riffs (concatenated) drive a compressor that pushes
    # the original track down while a riff is active, then recovers smoothly
    riff_inputs = "".join(f"[r{i}]" for i in range(1, len(placements) + 1))
    duck_amount = max(0.0, min(0.95, float(job.get("duck_amount", 0.65))))
    duck_ratio = 1.0 + duck_amount * 9.0
    parts.append(f"{riff_inputs}amix=inputs={len(placements)}:normalize=0,"
                 "alimiter=limit=0.95[sc]")
    parts.append(f"[{source_audio_idx}:a]anull[a1]")
    parts.append(f"[sc]asplit=2[sc_d][sc_mix]")
    parts.append(f"[a1][sc_d]sidechaincompress="
                 f"threshold=-30dB:ratio={duck_ratio:.2f}:attack=80:release=400:makeup=1.0[ducked]")
    parts.append(f"[ducked][sc_mix]amix=inputs=2:normalize=0,"
                 "alimiter=limit=0.97[aout]")
    if overlay_src:
        overlay_eof = ("shortest=1:eof_action=endall"
                       if overlay_src.suffix == ".webm" else "eof_action=repeat")
        parts.append(f"[0:v][{tidx}:v]overlay=x=(W-w)/2:y=H-h:{overlay_eof}[vout]")

    fc = ";".join(parts)
    cmd = [config.tool("ffmpeg"), "-y", "-v", "error", *inputs, "-filter_complex", fc]
    if overlay_src:
        cmd += ["-map", "[vout]", "-shortest"]
    else:
        cmd += ["-map", "0:v"]
    if overlay_src and overlay_src.suffix == ".webm":
        cmd += ["-shortest"]
    cmd += ["-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(job["crf"]),
            "-c:a", "aac", "-b:a", "128k", str(out)]
    subprocess.run(cmd, check=True)
    return {"video": out, "srt": srt}
