"""Stage 4b: the head writer's desk — audit riffs against context, rewrite the weak.

The judge is the final line between the writer's draft and the synth stage.
It answers: given the transcript before/after, the frames around the gap,
and the previous riff ... does this land? If not, it hands the writer back
a concrete critique for one rewrite attempt.

The judge sees the SAME bundle the writer saw, plus the riff — no surprises.
"""
import json
from pathlib import Path

from . import llm

JUDGE_SYSTEM = """You are THE HEAD WRITER — the judge sitting behind the writers' table.
For each draft riff, decide whether it lands.

YOU SEE:
- the frames around the gap (pre/mid/post or hot) as images,
- the transcript before/over/after that location,
- the previous riff (may be ""),
- the draft riff for this gap.

CRITERIA (in order):
1. CONTEXTUAL FIT: does the riff reference something the viewer just heard
   or saw? Callbacks rate highly. Generic jokes ("look at this", "so dramatic")
   rate low.
2. TIMING: does `when` make sense? Positive = land after a setup (good);
   negative = riff before the pause ends (good when the punchline compounds the setup).
3. WORD ECONOMY: budget_words is enforced; shorter is almost always better.
4. NO EXPLAINING: the joke should not label itself ("this is funny because...").
5. CONTINUITY: avoid contradicting the previous riff's premise.

Return ONLY a JSON array, one entry per draft riff in the same order:
[
  {"gap": <n>, "verdict": "keep"|"rewrite",
   "critique": "<if rewrite: the specific reason and what to change; otherwise \"\">",
   "score": <0..10 integer, 10 = nails the beat>}
]
Keep critiques terse and actionable."""


def judge_riffs(job: dict, riffs: list[dict], bundles: list[dict]) -> list:
    """Audit riffs. Returns list of dicts {gap,verdict,critique,score}."""
    if not riffs:
        return []
    bundle_by_gap = {b["gap"]["id"]: b for b in bundles}
    frames_dir = job["dir"] / "frames"

    user_parts = []
    for r in riffs:
        g = next((gg for gg in (b["gap"] for b in bundles) if gg["id"] == r["gap"]), None)
        if not g:
            continue
        b = bundle_by_gap[r["gap"]]
        # prev riff lookup: previous riff by gap ordering
        prev = ""
        sorted_riffs = sorted(riffs, key=lambda x: x["gap"])
        idx = sorted_riffs.index(r)
        if idx > 0:
            prev = sorted_riffs[idx - 1]["line"]

        user_parts.append({"type": "text", "text":
            f"=== DRAFT GAP {r['gap']} ===\n"
            f"gap@{g['start']:.1f}s, usable={g['usable']:.1f}s, budget={g['budget_words']}w\n"
            f"when={r.get('when', 0.0)}\n"
            f"draft:  '{r['line']}'\n"
            f"prev:   '{prev}'"})
        for tag, p in b["frames"].items():
            f = Path(p)
            if f.exists():
                user_parts.append({"type": "text", "text": f"[frame:{tag}]"})
                user_parts.append({"type": "image_url", "image_url":
                                   {"url": f"data:image/png;base64,{llm.b64_image(f)}"}})

        def fmt(lines):
            return "; ".join(f"{l['start']:.1f}s '{l['text']}'" for l in lines) or "(none)"
        user_parts.append({"type": "text", "text":
            f"transcript before: {fmt(b['transcript_before'])}\n"
            f"transcript over:   {fmt(b['transcript_over'])}\n"
            f"transcript after:  {fmt(b['transcript_after'])}\n"})

    response = llm.chat_json(job, JUDGE_SYSTEM, user_parts,
                             temperature=0.3, max_tokens=2000, role="judge")
    if not isinstance(response, list):
        return [{"gap": r["gap"], "verdict": "keep", "critique": "", "score": 6}
                for r in riffs]
    # normalize: only return entries that match an asked gap, in order
    by_gap = {r["gap"]: r for r in response if isinstance(r, dict) and "gap" in r}
    return [by_gap.get(r["gap"], {"gap": r["gap"], "verdict": "keep",
                                  "critique": "", "score": 5})
            for r in riffs]
