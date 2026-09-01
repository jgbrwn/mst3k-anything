"""Stage 4: write dense, evidence-first theater riffs.

The writer receives a complete transcript/callback ledger plus local visual evidence.
Silence is a useful landing cue, not a prerequisite, and the timing envelope is only a
preference: intentional dialogue overlap is part of the format.
"""

import base64
import json
import math
from pathlib import Path

from . import llm
from .cache import file_signature, value_digest


DRAFT_POLICY_VERSION = 3
MECHANISMS = {
    "observation", "literalization", "comparison", "mock_narration",
    "character_address", "production_critique", "sound_button", "escalation",
    "wordplay", "anti_joke", "callback",
}
TIMINGS = {"cue", "button", "overlap"}

SOLO_PERSONA = """You are THE RIFFER: an original, dry theater commentator watching a
specific video with an audience. Do not imitate named fictional characters, actors,
voices, catchphrases, or source dialogue. Preserve the original speaker identity:
`riffer`.

MISSION
Make every line feel discovered at this exact moment in this exact video. The joke must
come from a concrete visual, spoken phrase, edit, prop, gesture, contradiction, sound,
or recurring detail—not from generic heckling.

PRIORITIES
1. SPECIFICITY: name the actual object, action, expression, caption, location, wording,
   edit, or relationship visible or spoken in the supplied evidence. Do not invent.
2. COMIC TURN: transform the detail with a dry comparison, literal reading, escalation,
   reversal, mock institutional language, precise button, or character address.
3. CONTINUITY: treat the whole batch as one performance. Establish concrete details
   early; when one returns, callback to it and mutate or escalate it. Never force a
   callback without a real recurrence.
4. DENSITY: write a riff for every offered cue by default. Leave it silent only when the
   supplied evidence gives no defensible comic angle or the interruption would destroy
   an unusually important reveal.
5. TIMING: a riff may be a fragment, sentence, or two-beat line. Use extra words when
   the second beat creates a turn or callback; never pad just to fill time.
6. OVERTALK: deliberate overlap is allowed for asides, setups, reactions, reveals,
   buttons, and callbacks. Mark it as intentional. Never reject a good joke merely
   because dialogue is present or the preferred cue is short.
7. VOICE: dry, conversational, alert, slightly incredulous. Prefer precise understatement
   over broad yelling or generic melodrama.
8. TARGET: punch at the video's choices, construction, logic, props, pacing, editing,
   claims, or fictional behavior—not real private people or protected classes.

REJECT
- Generic filler that could fit any video: "here we go", "look at this", "so dramatic",
  "well, that happened", "apparently", or "and now..." unless the exact wording is
  itself the joke.
- Narration that merely describes what the viewer can already see.
- Explanations, disclaimers, hashtags, emojis, stage directions, or labels.
- Invented names, motives, dialogue, off-screen events, or visual details.
- Repeating a noun, sentence shape, or mechanism without a deliberate callback.

SET RHYTHM
Vary mechanisms across adjacent lines: precise observation, literalization, comparison,
mock narration, character address, production critique, sound/button, escalation, wordplay,
antijoke, and callback. A callback must reuse a concrete earlier detail while changing its
meaning.

PERFORMANCE SHORTHAND
`*word*` means slight stress; a final `...` means a trailing pause; final `!` or `?`
changes vocal contour. Use at most one accent per line."""

REGISTER = {
    "movie": "Inspect blocking, continuity, geography, props, costumes, acting choices, editing, and the gap between the scene's seriousness and its execution.",
    "tv": "Look for episode machinery: repeated setups, recap logic, recycled locations, artificial cliffhangers, and suspiciously efficient plot turns.",
    "vlog": "Target framing, room, camera habits, editing choices, topic pivots, props, and the mismatch between presentation and subject.",
    "tutorial": "Use deadpan precision: literal readings, suspiciously confident claims, missing steps, unsafe implications, and the object behaving unlike the narration.",
    "gaming": "Use exact play-by-play: UI wording, impossible stakes, repeated failure states, NPC behavior, and the gap between epic framing and routine input.",
    "music": "Land on cuts, poses, visual motifs, supplied lyric fragments, and the video's treatment of the song. Do not attack a real performer personally.",
    "home": "Use affectionate, observant teasing about framing, timing, props, family-video logic, and accidental production choices. Never be cruel.",
    "commercial": "Treat every claim, product shot, slogan, and visual promise as evidence in an overconfident sales presentation.",
    "other": "Use exact visual or spoken detail, a dry comic turn, occasional callback, and no generic heckling.",
}


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _fmt_lines(lines: list[dict]) -> str:
    return "; ".join(
        f"{line.get('start', 0):.1f}s [{line.get('text', '')}]" for line in lines
    ) or "(none)"


def _bundle_to_user(bundle: dict) -> list[dict]:
    """Encode one cue's frames and local setup/payoff evidence."""
    gap = bundle["gap"]
    anchor = gap.get("anchor", (gap["start"] + gap["end"]) / 2)
    source = gap.get("kind", "cue")
    txt = (
        f"=== CUE {gap['id']} ===\n"
        f"anchor={anchor:.1f}s; preferred={gap['start']:.1f}-{gap['end']:.1f}s; "
        f"source={source}; preferred_words={gap.get('budget_words', 8)}; "
        f"intentional_dialogue_overlap={'allowed' if gap.get('overlap_allowed', True) else 'not preferred'}\n"
        "Use the evidence below. The cue is a landing suggestion, not a silence requirement."
    )
    parts = [{"type": "text", "text": txt}]
    frame_times = bundle.get("frame_times", {})
    for tag, path in bundle.get("frames", {}).items():
        f = Path(path)
        if not f.exists():
            continue
        t = frame_times.get(tag, "?")
        parts.append({"type": "text", "text": f"[frame:{tag} at {t}s]"})
        parts.append({"type": "image_url", "image_url":
                      {"url": f"data:image/png;base64,{_b64(f)}"}})
    parts.append({"type": "text", "text":
                  "TRANSCRIPT setup/before:\n" + _fmt_lines(bundle.get("transcript_before", [])) +
                  "\nTRANSCRIPT at cue:\n" + _fmt_lines(bundle.get("transcript_over", [])) +
                  "\nTRANSCRIPT payoff/after:\n" + _fmt_lines(bundle.get("transcript_after", []))})
    if bundle.get("hot_moment") is not None:
        parts.append({"type": "text", "text":
                      f"[audio:hot moment near {bundle['hot_moment']:.1f}s]"})
    return parts


def _full_transcript(job: dict, bundles: list[dict]) -> str:
    path = job["dir"] / "transcript.json"
    lines = []
    if path.exists():
        try:
            lines = json.loads(path.read_text()).get("lines", [])
        except (OSError, json.JSONDecodeError):
            lines = []
    if not lines:
        for bundle in bundles or []:
            lines.extend(bundle.get("transcript_before", []))
            lines.extend(bundle.get("transcript_over", []))
            lines.extend(bundle.get("transcript_after", []))
    seen = set()
    out = []
    for line in sorted(lines, key=lambda item: item.get("start", 0)):
        key = (round(float(line.get("start", 0)), 2), line.get("text", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{float(line.get('start', 0)):.1f}s  {line.get('text', '')}")
    # Full context matters for callbacks, but keep pathological transcripts from
    # crowding out the actual cue frames in small-context providers.
    return "\n".join(out)[:60000]


def _profile_context(profile: dict) -> str:
    keys = ("kind", "tone", "premise", "characters", "targets", "visual_gags",
            "running_gags", "visual_motifs", "scene_beats", "do_not_target",
            "style_guide")
    return json.dumps({key: profile.get(key) for key in keys if key in profile},
                      ensure_ascii=False, indent=2)[:18000]


def _system_prompt(profile: dict) -> str:
    kind = (profile or {}).get("kind", "other")
    return (SOLO_PERSONA + "\n\nVIDEO REGISTER\n" + REGISTER.get(kind, REGISTER["other"]) +
            "\n\nCONTINUITY PROFILE\n" + _profile_context(profile or {}))


def _output_contract() -> str:
    return """Return ONLY a JSON array with exactly one object for every offered cue, in the same order.
Do not omit, duplicate, reorder, or invent cue IDs.
[
  {
    "gap": 12,
    "speaker": "riffer",
    "status": "riff",
    "line": "spoken words only",
    "when": 0.0,
    "timing": "cue",
    "mechanism": "observation",
    "evidence": ["frame:mid", "transcript:setup:0"],
    "callback_to": null
  }
]

`status` is `riff` or `silence`; use silence only for a genuinely unusable cue.
`when` is signed seconds relative to the cue anchor. Negative values are intentional
overtalk/setup and require `timing: "overlap"`. Positive values can land a button after
the setup. `timing` is `cue`, `button`, or `overlap`. `mechanism` is one of:
observation, literalization, comparison, mock_narration, character_address,
production_critique, sound_button, escalation, wordplay, anti_joke, callback.
`evidence` must cite one or two supplied frame/transcript/profile references.
`callback_to` is an earlier cue ID only for a real callback; otherwise null.
A preferred word count is guidance, not a hard rejection rule. Do not output prose,
markdown, stage directions, or emojis outside the JSON."""


def _finite_when(value) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalize_response(response, gaps: list[dict], bundles: list[dict], job: dict) -> list[dict]:
    if isinstance(response, dict):
        response = response.get("riffs") or response.get("cues") or response.get("items") or []
    if not isinstance(response, list):
        return []
    allowed = {int(g["id"]): g for g in gaps}
    bundle_by_gap = {int(b["gap"]["id"]): b for b in bundles or []}
    out = []
    seen = set()
    for item in response:
        if not isinstance(item, dict):
            continue
        try:
            gid = int(item.get("gap"))
        except (TypeError, ValueError):
            continue
        if gid not in allowed or gid in seen:
            continue
        seen.add(gid)
        line = " ".join(str(item.get("line") or "").split()).strip()
        status = str(item.get("status") or ("riff" if line else "silence")).lower()
        if status == "silence" or not line:
            continue
        if "```" in line or len(line) > int(job.get("max_line_chars", 240)):
            continue
        timing = str(item.get("timing") or ("overlap" if _finite_when(item.get("when")) < 0 else "cue"))
        if timing not in TIMINGS:
            timing = "cue"
        when = _finite_when(item.get("when", 0.0))
        if when < 0:
            timing = "overlap"
        mechanism = str(item.get("mechanism") or "observation").lower()
        if mechanism not in MECHANISMS:
            mechanism = "observation"
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            evidence = (bundle_by_gap.get(gid) or {}).get("candidate_evidence") or ["frame:mid"]
        evidence = [str(ref)[:80] for ref in evidence[:2]]
        callback = item.get("callback_to")
        try:
            callback = int(callback) if callback is not None else None
        except (TypeError, ValueError):
            callback = None
        if callback is None or callback <= 0 or callback >= gid:
            callback = None
        if mechanism == "callback" and callback is None:
            mechanism = "observation"
        out.append({"gap": gid, "speaker": "riffer", "status": "riff", "line": line,
                    "words": len(line.split()), "when": when, "timing": timing,
                    "mechanism": mechanism, "evidence": evidence,
                    "callback_to": callback})
    return sorted(out, key=lambda item: item["gap"])


def _batch(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _draft_policy(job: dict, gaps: list[dict], profile: dict) -> dict:
    return {
        "version": DRAFT_POLICY_VERSION,
        "gaps": [{"id": int(g["id"]), "anchor": round(float(g.get("anchor", 0)), 3),
                  "start": round(float(g["start"]), 3), "end": round(float(g["end"]), 3)}
                 for g in gaps],
        "profile": value_digest(profile or {}),
        "transcript": file_signature(job["dir"] / "transcript.json"),
    }


def _draft_cache_valid(cache: Path, marker: Path, policy: dict) -> bool:
    if not cache.exists() or not marker.exists():
        return False
    try:
        cached_policy = json.loads(marker.read_text())
        data = json.loads(cache.read_text())
        return cached_policy == policy and isinstance(data, list)
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def _write_drafts(job: dict, gaps: list[dict], profile: dict,
                  bundles: list[dict] | None = None,
                  critique_context: str = "", only_gap: int | None = None) -> list[dict]:
    initial = not critique_context and only_gap is None
    cache = job["dir"] / "drafts.json"
    marker = job["dir"] / "drafts_policy.json"
    policy = _draft_policy(job, gaps, profile or {})
    if initial and _draft_cache_valid(cache, marker, policy):
        return json.loads(cache.read_text())

    selected = [g for g in gaps if only_gap is None or int(g["id"]) == int(only_gap)]
    bundle_by_gap = {int(b["gap"]["id"]): b for b in bundles or []}
    system = _system_prompt(profile or {})
    common = (
        "\n\nWHOLE-VIDEO TRANSCRIPT FOR CALLBACKS\n" + (_full_transcript(job, bundles or []) or "(none)") +
        "\n\nOUTPUT CONTRACT\n" + _output_contract()
    )
    previous = []
    all_out = []
    batch_size = max(1, int(job.get("writer_batch_size", 8)))
    for batch in _batch(selected, 1 if critique_context else batch_size):
        summary = "\n".join(
            f"CUE {g['id']} anchor {g.get('anchor', g['start']):.1f}s; "
            f"preferred {g['start']:.1f}-{g['end']:.1f}s; source {g.get('kind', 'cue')}; "
            f"preferred words {g.get('budget_words', 8)}"
            for g in batch
        )
        prompt_text = (
            ("DIRECTOR'S NOTE\n" + critique_context + "\n" if critique_context else "") +
            f"Write one grounded riff for each of these {len(batch)} offered cues.\n{summary}\n\n"
            "Plan the set before answering: establish concrete details, vary comic mechanisms, "
            "and use a callback only when the whole-video evidence supports it. "
            "Default to a riff, not silence."
        )
        user = [{"type": "text", "text": prompt_text}]
        if previous:
            user.append({"type": "text", "text":
                         "EARLIER RIFF LEDGER (use only for real callbacks; do not repeat):\n" +
                         "\n".join(previous[-12:])})
        user.append({"type": "text", "text": common})
        for gap in batch:
            bundle = bundle_by_gap.get(int(gap["id"]))
            if bundle:
                user.extend(_bundle_to_user(bundle))
            else:
                user.append({"type": "text", "text":
                             f"CUE {gap['id']} at {gap.get('anchor', gap['start']):.1f}s; use available frame evidence."})
                frame = job["dir"] / "frames" / f"gap{gap['id']:03d}.png"
                if frame.exists():
                    user.append({"type": "image_url", "image_url":
                                 {"url": f"data:image/png;base64,{_b64(frame)}"}})
        response = llm.chat_json(job, system, user, temperature=0.85,
                                 max_tokens=max(1200, len(batch) * 260))
        normalized = _normalize_response(response, batch, bundles or [], job)
        # A second constrained call recovers omitted cues without weakening the
        # main prompt or inventing filler in the successful results.
        missing = [g for g in batch if int(g["id"]) not in {r["gap"] for r in normalized}]
        if missing:
            repair_user = [{"type": "text", "text":
                             (("DIRECTOR'S NOTE\n" + critique_context + "\n") if critique_context else "") +
                             "The prior answer omitted these cue IDs: " +
                             ", ".join(str(g["id"]) for g in missing) +
                             ". Return exactly one riff object for each omitted ID. "
                             "Use silence only if evidence is genuinely unusable.\n" + common}]
            for gap in missing:
                bundle = bundle_by_gap.get(int(gap["id"]))
                if bundle:
                    repair_user.extend(_bundle_to_user(bundle))
            repaired = llm.chat_json(job, system, repair_user, temperature=0.75,
                                     max_tokens=max(700, len(missing) * 220))
            normalized.extend(_normalize_response(repaired, missing, bundles or [], job))
            normalized.sort(key=lambda item: item["gap"])
        for item in normalized:
            previous.append(f"cue {item['gap']}: {item['line']}")
        all_out.extend(normalized)

    all_out.sort(key=lambda item: item["gap"])
    if initial:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_out, indent=2, ensure_ascii=False))
        tmp.replace(cache)
        marker.write_text(json.dumps(policy, indent=2))
    return all_out


def write_riffs_with_review(job: dict, gaps: list[dict], profile: dict,
                             bundles: list[dict] | None = None) -> list[dict]:
    """Draft every cue, judge in batches, and rewrite salvageable weak lines."""
    from . import judge
    drafts = _write_drafts(job, gaps, profile, bundles, critique_context="")
    print(f"    [judge] reviewing {len(drafts)} drafts")
    try:
        verdicts = judge.judge_riffs(job, drafts, bundles or [], profile=profile)
    except TypeError:
        # Compatibility with a third-party/test judge using the old signature.
        try:
            verdicts = judge.judge_riffs(job, drafts, bundles or [])
        except Exception as exc:
            print(f"    [judge] unavailable ({exc}); keeping drafts", flush=True)
            verdicts = []
    except Exception as exc:
        print(f"    [judge] unavailable ({exc}); keeping drafts", flush=True)
        verdicts = []

    by_gap = {int(v.get("gap")): v for v in verdicts or [] if isinstance(v, dict) and v.get("gap") is not None}
    out = []
    for draft in drafts:
        verdict = by_gap.get(draft["gap"], {"verdict": "keep", "score": 8, "critique": ""})
        decision = str(verdict.get("verdict") or "keep").lower()
        try:
            score = int(verdict.get("score", 8))
        except (TypeError, ValueError):
            score = 8
        if decision == "keep" and score < 8:
            decision = "rewrite"
            verdict = dict(verdict)
            verdict["critique"] = verdict.get("critique") or (
                "Make this more specific to the supplied frame or transcript and add a "
                "clear comic turn; do not settle for a generic description.")
        print(f"    [judge] gap{draft['gap']:2d} score={verdict.get('score', '?')} {decision}")
        if decision == "rewrite" and verdict.get("critique"):
            rewrite = _write_drafts(job, gaps, profile, bundles,
                                    critique_context=_critique_for(draft, verdict, bundles, profile),
                                    only_gap=draft["gap"])
            match = next((item for item in rewrite if item["gap"] == draft["gap"]), None)
            if match:
                match["_kept_from_rewrite"] = True
                match["_critique"] = verdict.get("critique", "")
                out.append(match)
                continue
        if decision == "drop":
            continue
        draft["_judge_score"] = verdict.get("score")
        draft["_judge_verdict"] = decision
        out.append(draft)

    judged_path = job["dir"] / "judged_riffs.json"
    tmp = judged_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.replace(judged_path)
    return out


def _critique_for(riff: dict, verdict: dict, bundles: list,
                  profile: dict | None = None) -> str:
    b = next((bundle for bundle in bundles or []
              if bundle.get("gap", {}).get("id") == riff["gap"]), None)
    if not b:
        return (f"Rewrite cue {riff['gap']}. Previous draft: {riff['line']}. "
                f"Director's note: {verdict.get('critique', '')}")
    return (
        f"Rewrite cue {riff['gap']} using the supplied evidence.\n"
        f"Previous draft: {riff['line']}\n"
        f"Director's note: {verdict.get('critique', '')}\n"
        f"Setup: {_fmt_lines(b.get('transcript_before', []))}\n"
        f"At cue: {_fmt_lines(b.get('transcript_over', []))}\n"
        f"Payoff: {_fmt_lines(b.get('transcript_after', []))}\n"
        "Keep the strongest concrete detail, add an actual comic turn, and preserve a "
        "deliberate overlap timing when that is the better landing.")


def budget_for(gaps: list[dict], gid) -> int:
    """Compatibility helper: preferred words, never a hard validation gate."""
    for gap in gaps:
        if gap["id"] == gid:
            return int(gap.get("budget_words", 8))
    return 8
