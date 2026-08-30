"""Stage 4: the writers' room — turn gaps + profile into riffs via chat completions."""
import base64
import json
from pathlib import Path

from . import llm

SOLO_PERSONA = """You are THE RIFFER — a lone robot heckler in a theater, watching this
video with the audience. In the MST3K tradition: quick, witty, never mean to real
people, punching at the thing on screen.

STYLE RULES
1. One short line per gap. Never exceed the word budget — talking over dialogue is
   the one unforgivable sin.
2. Riff about THE SETUP: what was just said (transcript before), what's on screen
   now (frame:mid), and/or what happens right after (transcript after / frame:post).
   A riff that ignores the 5 seconds before it lands isn't riffing, it's narration.
   Callbacks to your own previous riffs are gold when the context repeats.
3. Vary the joke types: observations, callbacks, literal readings, fake narration,
   addressing a character directly, audience asides.
4. Conversational and fast — like a friend riffing next to you, not standup.
5. No explaining the joke, no emojis, no stage directions. Spoken words only.

PERFORMANCE SHORTHAND (the voice actor uses this):
- *word* = stress that word slightly (slowed, pitch-dropped)
- ending "..." / "…" = trailing-off pause
- ending "!" or "?" = lift the end (surprise/questions)
Use at most one per riff — these are musical accents, not directions."""

REGISTER = {
    "movie": "Attack production values, continuity, acting, writing. Classic bad-movie riffing.",
    "tv": "Same as movie; also poke at episode structure and recycled plots.",
    "vlog": "Tease the creator's choices: the room, the editing, the topic leaps, the props.",
    "tutorial": "Deadpan corrections, over-literal questions, mock concern for safety.",
    "gaming": "Play-by-play heckling, exaggerated stakes, mocking the UI/HUD.",
    "music": "Riffs that land on beats/cuts; mock the video more than the song.",
    "home": "Warm teasing — like a friend roasting a home movie, never cruel.",
    "commercial": "Sell them harder than they sell themselves; fake enthusiasm.",
    "other": "General MST3K-style heckling of whatever is happening on screen.",
}


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _bundle_to_user(bundle: dict, prev_riff: str):
    """One bundle -> list of chat-completion content parts."""
    g = bundle["gap"] = bundle["gap"]
    tag = f", score={g.get('score', 0):.2f}" if "score" in g else ""
    txt = (f"GAP {g['id']} at {g['start']}s ({g['usable']}s usable{tag}, "
           f"budget {g['budget_words']} words), kind={g.get('kind','?')}")
    parts = [{"type": "text", "text": txt}]

    for tag, path in bundle["frames"].items():
        f = Path(path)
        if f.exists():
            parts.append({"type": "text", "text": f"[frame:{tag}]"})
            parts.append({"type": "image_url", "image_url":
                          {"url": f"data:image/png;base64,{_b64(f)}"}})

    def fmt(lines):
        return "; ".join(f"{l['start']:.1f}s '{l['text']}'" for l in lines) or "(none)"

    parts.append({"type": "text", "text":
        f"TRANSCRIPT before: {fmt(bundle['transcript_before'])}\n"
        f"TRANSCRIPT over:   {fmt(bundle['transcript_over'])}\n"
        f"TRANSCRIPT after:  {fmt(bundle['transcript_after'])}"})
    if bundle.get("hot_moment") is not None:
        parts.append({"type": "text", "text":
            f"HOT MOMENT near {bundle['hot_moment']:.1f}s — something dramatic here."})
    if prev_riff:
        parts.append({"type": "text", "text": f"PREV RIFF (may callback): '{prev_riff}'"})
    return parts


def write_riffs(job: dict, gaps: list[dict], profile: dict,
                bundles: list[dict] | None = None) -> list[dict]:
    cache = job["dir"] / "riffs.json"
    if cache.exists():
        return json.loads(cache.read_text())
    frames = job["dir"] / "frames"
    kind = (profile or {}).get("kind", "other")
    register = REGISTER.get(kind, REGISTER["other"])

    system = SOLO_PERSONA + "\n\nREGISTER FOR THIS VIDEO:\n" + register
    if profile:
        system += ("\n\nCONTENT PROFILE:\n"
                   f"kind: {profile.get('kind')}\n"
                   f"tone: {profile.get('tone')}\n"
                   f"premise: {profile.get('premise')}\n"
                   f"targets: {profile.get('targets')}")
        if profile.get("style_guide"):
            system += f"\nSTYLE GUIDE: {profile.get('style_guide')}"
    hot_path = Path(job["dir"]) / "hot_moments.json"
    if hot_path.exists():
        hot = json.loads(hot_path.read_text())
        if hot:
            hits = [str(h) for h in hot]
            system += ("\n\nHOT MOMENTS (audio suggests something dramatic happens here; "
                       "the joke is stronger if it lands around these): " + ", ".join(hits) + "s")
    system += ('\n\nReturn ONLY a JSON array: '
               '[{"gap": <n>, "line": "..."}, ...] one entry per offered gap, '
               'in order. Empty string "" is allowed if no good riff exists.')

    user = []
    prev_riff = ""
    if bundles:
        bundle_by_gap = {b["gap"]["id"]: b for b in bundles}
        for g in gaps:
            user.extend(_bundle_to_user(bundle_by_gap[g["id"]], prev_riff))
            # prev_riff wires in the previous gap's line next iteration
            # (filled post-hoc here by peeking; ok for ordering-only)
            prev_riff = ""  # reset; full riff-chaining happens once written
    else:
        for g in gaps:
            tag = f", score={g.get('score', 0):.2f}" if "score" in g else ""
            user.append({"type": "text", "text":
                f"GAP {g['id']} at {g['start']}s ({g['usable']}s usable{tag}, "
                f"budget {g['budget_words']} words). Frame:"})
            f = frames / f"gap{g['id']:03d}.png"
            if f.exists():
                user.append({"type": "image_url", "image_url":
                             {"url": f"data:image/png;base64,{_b64(f)}"}})

    riffs = llm.chat_json(job, system, user, temperature=0.9, max_tokens=2000)
    out = []
    for r in riffs:
        line = (r.get("line") or "").strip()
        words = len(line.split())
        if line and words <= budget_for(gaps, r.get("gap")):
            out.append({"gap": int(r["gap"]), "speaker": "riffer",
                        "line": line, "words": words})
    cache.write_text(json.dumps(out, indent=2))
    return out


def budget_for(gaps: list[dict], gid) -> int:
    for g in gaps:
        if g["id"] == gid:
            return g["budget_words"]
    return 4
