"""Fit-check riffs against gaps, duck+mix surviving ones, overlay theater."""
import subprocess, json, pathlib
GAPS = {1: (75.37, 1.26), 2: (161.36, 4.40), 3: (176.39, 1.45)}
MARGIN = 0.35  # dead air each end, per movie-sign
placements = []
for gid, (t, dur) in GAPS.items():
    wav = pathlib.Path(f"/tmp/p9/riffs/r{gid}.wav")
    d = float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",str(wav)],capture_output=True,text=True).stdout.strip())
    usable = dur - 2*MARGIN
    if d <= usable:
        placements.append((t + MARGIN, wav))
        print(f"gap{gid}: FITS ({d:.2f}s <= {usable:.2f}s) -> place at {t+MARGIN:.2f}s")
    else:
        print(f"gap{gid}: DROP ({d:.2f}s > {usable:.2f}s)")

# Build ducked audio + riff track in one ffmpeg graph
filters, inputs = [], ["-i", "/tmp/p9/plan9_short.mp4"]
for i, (t, wav) in enumerate(placements):
    inputs += ["-i", str(wav)]
for i, (t, wav) in enumerate(placements):
    filters.append(f"[{i+1}:a]adelay={int(t*1000)}|{int(t*1000)},volume=1.4[r{i}]")
mix = "[0:a]volume=1.0,duck=0:0" # placeholder; we'll chain sidechained volume manually
# Simpler robust approach: lower base during riff windows with volume timeline expressions
vol_expr = "1.0"
for (t, wav) in placements:
    d = float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",str(wav)],capture_output=True,text=True).stdout.strip())
    vol_expr += f"-0.65*between(t,{t},{t+d})"
audio_chain = f"[0:a]volume={vol_expr}:eval=frame[base]"
mix_inputs = "[base]" + "".join(f"[r{i}]" for i in range(len(placements)))
filters.append(f"{mix_inputs}amix=inputs={len(placements)+1}:normalize=0[aout]")
vf = "[0:v][2:v]overlay=0:main_h-overlay_h[vout]" if False else None
graph = ";".join(filters) + f";{audio_chain};{mix_inputs}amix=inputs={len(placements)+1}:normalize=0[aout]"
# cleaner: build once
filters2 = []
for i, (t, wav) in enumerate(placements):
    filters2.append(f"[{i+1}:a]adelay={int(t*1000)}|{int(t*1000)},volume=1.4[r{i}]")
filters2.append(f"[0:a]volume={vol_expr}:eval=frame[base]")
mi = "[base]" + "".join(f"[r{i}]" for i in range(len(placements)))
filters2.append(f"{mi}amix=inputs={len(placements)+1}:normalize=0[aout]")
graph = ";".join(filters2)
cmd = (["ffmpeg","-y","-v","error"] + inputs + ["-filter_complex", graph,
       "-map","[aout]","-map","0:v","-c:v","copy","-c:a","aac","-shortest",
       "/tmp/p9/final_riffed_v2.mp4"])
print("placements:", len(placements))
subprocess.run(cmd, check=True)
print("rendered /tmp/p9/final_riffed_v2.mp4")
