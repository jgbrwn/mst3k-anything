"""Stage 4b: audit riffs for grounded comic writing, timing, and continuity."""

import json
from pathlib import Path

from . import llm


JUDGE_BATCH_SIZE = 6

JUDGE_SYSTEM = """You are the comedy editor for an original theater-riffing show. Judge each
draft as writing for this exact video, not as a generic caption. Do not imitate named
fictional characters, actors, voices, catchphrases, or source dialogue.

The format is lively and dense. Dialogue overlap is allowed when intentional: a quick
aside, setup, reaction, reveal, button, or callback can land over the source. Do not
penalize a riff merely because the cue is not silent or the line is longer than the
preferred delivery window.

AUDIENCE KNOWLEDGE
The riffer is a regular watcher, not an omniscient narrator. For each cue, only source
transcript, frames, and profile evidence available at or before that cue may justify the
line. A later reveal, action, frame, or transcript line is a forward reference and must
be rewritten. A natural reaction usually starts after the beat; a line that starts before
it must be an explicitly intentional setup/overlap.

For every draft evaluate:
1. GROUNDEDNESS (0-3): concrete supplied visual, spoken phrase, edit, prop, gesture,
   sound, or earlier documented motif.
2. COMIC TURN (0-3): transformation, comparison, escalation, reversal, literal reading,
   precise button, or actual callback. Description alone scores low.
3. VOICE (0-2): dry, conversational, off-the-cuff, punchy, and natural rather than a
forced detail list or prepared essay.
4. TIMING (0-2): reaction after the beat or clearly intentional overlap; no prediction.

A draft that could be transplanted to another video is not a keep. Prefer `rewrite` for
anything below 8/10 when salvageable; keep only a line with a concrete comic turn and
adequate grounding. Use `drop` only when no grounded joke remains after rewriting—not
because of silence, length, or dialogue overlap.

Return ONLY a JSON array in input order:
[
  {"gap": 12, "verdict": "keep|rewrite|drop", "score": 0,
   "dimension_scores": {"groundedness": 0, "comic_turn": 0, "voice": 0, "timing": 0},
   "failure_codes": [], "critique": "", "evidence_ok": true, "callback_ok": true}
]
For rewrites, critique must name the weak phrase and the concrete evidence or comic move
the replacement should use. Flag forward references, premature starts, forced detail
lists, generic descriptions, and missing comic turns. Keep it actionable."""


def _fmt(lines: list[dict]) -> str:
    return "; ".join(
        f"{line.get('start', 0):.1f}s [{line.get('text', '')}]" for line in lines
    ) or "(none)"


def _profile_text(profile: dict | None) -> str:
    if not profile:
        return "(no continuity profile)"
    return json.dumps({key: profile.get(key) for key in (
        "premise", "characters", "running_gags", "visual_motifs", "targets", "scene_beats"
    ) if key in profile}, ensure_ascii=False)[:12000]


def _transcript_through(job: dict, timestamp: float) -> str:
    path = job["dir"] / "transcript.json"
    if not path.exists():
        return "(no transcript)"
    try:
        lines = json.loads(path.read_text()).get("lines", [])
    except (OSError, json.JSONDecodeError):
        return "(no transcript)"
    lines = [line for line in lines
             if float(line.get("end", line.get("start", 0))) <= float(timestamp) + 0.05]
    return "\n".join(f"{float(line.get('start', 0)):.1f}s {line.get('text', '')}"
                     for line in lines)[-20000:] or "(no transcript)"


def _all_draft_ledger(riffs: list[dict]) -> str:
    return "\n".join(
        f"cue {riff['gap']}: {riff['line']} "
        f"(mechanism={riff.get('mechanism', 'observation')}, timing={riff.get('timing', 'cue')})"
        for riff in sorted(riffs, key=lambda item: item["gap"])
    )


def _judge_one_batch(job: dict, drafts: list[dict], bundles: list[dict],
                     profile: dict | None, all_drafts: list[dict]) -> list[dict]:
    bundle_by_gap = {int(bundle["gap"]["id"]): bundle for bundle in bundles}
    user_parts = [{"type": "text", "text":
                   "CONTINUITY PROFILE\n" + _profile_text(profile) +
                   "\n\nCOMPLETE DRAFT LEDGER (ordered; callbacks must point backward)\n" +
                   _all_draft_ledger(all_drafts) +
                   "\n\nJudge the following batch. Return one verdict per cue, in order."}]
    for riff in drafts:
        bundle = bundle_by_gap.get(int(riff["gap"]))
        gap = bundle["gap"] if bundle else {}
        anchor = float(gap.get("anchor", gap.get("start", 0)))
        user_parts.append({"type": "text", "text":
                           f"=== DRAFT CUE {riff['gap']} ===\n"
                           f"anchor={anchor:.1f}s; preferred={gap.get('start', '?')}-"
                           f"{gap.get('end', '?')}s; preferred_words={gap.get('budget_words', '?')}\n"
                           f"draft={riff['line']}\n"
                           f"when={riff.get('when', 0.0)}; timing={riff.get('timing', 'cue')}; "
                           f"mechanism={riff.get('mechanism', 'observation')}; "
                           f"evidence={riff.get('evidence', [])}\n"
                           "SOURCE TRANSCRIPT AVAILABLE THROUGH THIS CUE:\n" +
                           _transcript_through(job, anchor)})
        if not bundle:
            continue
        for tag, path in bundle.get("frames", {}).items():
            frame = Path(path)
            if frame.exists():
                time_s = bundle.get("frame_times", {}).get(tag, "?")
                user_parts.append({"type": "text", "text": f"[frame:{tag} at {time_s}s]"})
                user_parts.append({"type": "image_url", "image_url":
                                   {"url": f"data:image/png;base64,{llm.b64_image(frame)}"}})
        user_parts.append({"type": "text", "text":
                           "TRANSCRIPT completed before cue: " +
                           _fmt(bundle.get("transcript_before", [])) +
                           "\nDo not use transcript_after or any future evidence."})
    response = llm.chat_json(
        job, JUDGE_SYSTEM, user_parts, temperature=0.25,
        max_tokens=max(1200, len(drafts) * 220), role="judge")
    return response if isinstance(response, list) else []


def judge_riffs(job: dict, riffs: list[dict], bundles: list[dict],
                profile: dict | None = None) -> list[dict]:
    """Audit the whole draft set in bounded batches and restore missing verdicts."""
    if not riffs:
        return []
    batch_size = max(1, int(job.get("writer_batch_size", JUDGE_BATCH_SIZE)))
    response = []
    for start in range(0, len(riffs), batch_size):
        response.extend(_judge_one_batch(job, riffs[start:start + batch_size], bundles,
                                         profile, riffs))
    by_gap = {}
    for value in response:
        if not isinstance(value, dict):
            continue
        try:
            gap = int(value.get("gap"))
        except (TypeError, ValueError):
            continue
        by_gap[gap] = value
    normalized = []
    for riff in riffs:
        verdict = dict(by_gap.get(riff["gap"], {
            "gap": riff["gap"], "verdict": "keep", "critique": "", "score": 8
        }))
        decision = str(verdict.get("verdict") or "keep").lower()
        if decision not in {"keep", "rewrite", "drop"}:
            decision = "keep"
        verdict["gap"] = riff["gap"]
        verdict["verdict"] = decision
        try:
            verdict["score"] = max(0, min(10, int(verdict.get("score", 8))))
        except (TypeError, ValueError):
            verdict["score"] = 8
        normalized.append(verdict)
    return normalized
