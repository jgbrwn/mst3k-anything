"""Stage 6: duck-and-mix original audio + riff track + theater overlay; emit SRT."""
import json
import subprocess

from .analyze import grab_frames  # noqa: F401  (re-export convenience)


def build(job: dict, placements: list[dict]) -> dict:
    """placements: [{start, wav, duration, gap_id, line}]. Returns paths."""
    out = job["dir"] / "final.mp4"
    srt = job["dir"] / "riffs.srt"

    # --- SRT of the riff track (read-along / verify artifact) ---
    def ts(s):
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")
    with open(srt, "w") as f:
        for i, p in enumerate(sorted(placements, key=lambda p: p["start"]), 1):
            f.write(f"{i}\n{ts(p['start'])} --> {ts(p['start'] + p['duration'])}\n"
                    f"{p['line']}\n\n")

    if not placements:
        # no riffs survived: still ship the overlaid video
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(job["source"])]
        vf = ""
        if (job["dir"] / "theater.png").exists():
            vf = f"[0:v][1:v]overlay=0:H-h[vout]"
            cmd += ["-i", str(job["dir"] / "theater.png"), "-filter_complex", vf,
                    "-map", "[vout]", "-map", "0:a?"]
        else:
            cmd += ["-c", "copy"]
        cmd.append(str(out))
        subprocess.run(cmd, check=True)
        return {"video": out, "srt": srt}

    # --- audio graph: duck original under each riff, riffs louder ---
    theater = job["dir"] / "theater.png"
    has_theater = theater.exists()

    inputs = ["-i", str(job["source"])]
    for p in placements:
        inputs += ["-i", str(p["wav"])]
    tidx = None
    if has_theater:
        tidx = len(placements) + 1
        inputs += ["-i", str(theater)]

    parts = []
    for i, p in enumerate(placements, start=1):
        delay_ms = int(p["start"] * 1000)
        parts.append(
            f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms},volume={job['riff_gain']:.2f}[r{i}]")
    # sidechain ducking: riffs (concatenated) drive a compressor that pushes
    # the original track down while a riff is active, then recovers smoothly
    riff_inputs = "".join(f"[r{i}]" for i in range(1, len(placements) + 1))
    parts.append(f"{riff_inputs}amix=inputs={len(placements)}:normalize=0[sc]")
    parts.append(f"[0:a]anull[a1]")
    parts.append(f"[sc]asplit=2[sc_d][sc_mix]")
    parts.append(f"[a1][sc_d]sidechaincompress=threshold=-30dB:ratio=6:attack=80:release=400:makeup=1.0[ducked]")
    parts.append(f"[ducked][sc_mix]amix=inputs=2:normalize=0[aout]")
    if has_theater:
        parts.append(f"[0:v][{tidx}:v]overlay=0:H-h[vout]")

    fc = ";".join(parts)
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc]
    if has_theater:
        cmd += ["-map", "[vout]"]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(job["crf"]),
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)]
    subprocess.run(cmd, check=True)
    return {"video": out, "srt": srt}
