"""Synthesize the 3 riffs with Pocket TTS, pitch-color each character."""
import subprocess, json, pathlib
TT = "/home/exedev/mst3k/tts-venv/bin/pocket-tts"
OUT = pathlib.Path("/tmp/p9/riffs")
OUT.mkdir(exist_ok=True)
voices = {
    "host":  {"voice": None, "pitch": 0.0},          # default alba -> keep neutral
    "crow":  {"voice": None, "pitch": -1.5},         # pitch down = raspier
    "servo": {"voice": None, "pitch": 4.0},          # pitch up = puppet territory
}
for r in json.load(open("/tmp/p9/riffs/riffs.json")):
    gid, spk, line = r["gap"], r["speaker"], r["line"]
    v = voices[spk]
    raw = OUT / f"r{gid}_raw.wav"
    fin = OUT / f"r{gid}.wav"
    cmd = [TT, "generate", "-q", "--text", line, "--output-path", str(raw)]
    if v["voice"]: cmd += ["--voice", v["voice"]]
    subprocess.run(cmd, check=True, capture_output=True)
    if v["pitch"] != 0.0:
        st = 2 ** (v["pitch"] / 12)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw),
                        "-af", f"atempo={st:.4f}", str(fin)], check=True)
    else:
        raw.rename(fin)
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(fin)], capture_output=True, text=True).stdout.strip()
    print(f"gap{gid} {spk}: '{line}' -> {fin.name} {d}s")
