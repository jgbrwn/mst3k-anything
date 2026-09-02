# Deadwood — Relentless comparison

These are two completed `mst3k-anything` runs of the same supplied Deadwood driving
video using the **Relentless** density setting (bias `4`). They make a useful qualitative
comparison of different multimodal writer/judge pairings.

| Variant | Writer | Judge | Planned cues | Rendered riffs | Judge rewrites |
|---|---|---|---:|---:|---:|
| [Luna](deadwood-relentless-riffed.mp4) | OpenRouter `openai/gpt-5.6-luna` | same | 27 | 27 | 8 |
| [Gemma + Grok](deadwood-relentless-gemma31b-grok46-riffed.mp4) | OpenRouter `google/gemma-4-31b-it` | OpenRouter `x-ai/grok-4.6` | 27 | 26 | 23 |

Both source videos are about 4:50 at 1280×720. The Gemma/Grok run's final cue reached
the physical video boundary and was omitted from the rendered manifest; the job itself
completed successfully.

## Posters

[![Luna poster](poster.jpg)](deadwood-relentless-riffed.mp4)
[![Gemma + Grok poster](poster-gemma31b-grok46.jpg)](deadwood-relentless-gemma31b-grok46-riffed.mp4)

## Files

- [`deadwood-relentless-riffed.mp4`](deadwood-relentless-riffed.mp4) — Luna video
- [`deadwood-relentless-riffs.srt`](deadwood-relentless-riffs.srt) — Luna subtitles
- [`deadwood-relentless-riffs.json`](deadwood-relentless-riffs.json) — Luna manifest
- [`deadwood-relentless-gemma31b-grok46-riffed.mp4`](deadwood-relentless-gemma31b-grok46-riffed.mp4) — Gemma/Grok video
- [`deadwood-relentless-gemma31b-grok46-riffs.srt`](deadwood-relentless-gemma31b-grok46-riffs.srt) — Gemma/Grok subtitles
- [`deadwood-relentless-gemma31b-grok46-riffs.json`](deadwood-relentless-gemma31b-grok46-riffs.json) — Gemma/Grok manifest

## Informal observation

On this clip, the Gemma 31B writer plus Grok 4.6 judge sounds roughly on par with the
Luna writer/judge run. That qualifies the earlier broad impression that Luna was clearly
best: model *pairs*, prompt state, and source material matter. This remains anecdotal,
not a controlled benchmark.

The videos are generated examples, not claims of ownership over the underlying source
footage. Use source material responsibly.
