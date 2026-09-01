import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mst3k import analyze, config, llm, transcribe, voice, writer
from mst3k.cli import write_rendered_manifest


class RenderManifestTests(unittest.TestCase):
    def test_manifest_contains_only_rendered_placements_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rendered_manifest(Path(tmp), [
                {"gap": 2, "line": "second", "start": 4.0, "duration": .8},
                {"gap": 1, "line": "first", "start": 1.0, "duration": .7},
            ])
            data = json.loads(path.read_text())
            self.assertEqual([item["line"] for item in data], ["first", "second"])
            self.assertEqual(set(data[0]), {"gap", "speaker", "line", "words",
                                            "when", "start", "duration"})

    def test_judge_output_is_not_written_as_public_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = {"dir": Path(tmp)}
            gaps = [{"id": 1, "start": 1.0, "usable": 2.0, "budget_words": 5}]
            bundles = [{"gap": gaps[0], "frames": {},
                        "transcript_before": [], "transcript_over": [],
                        "transcript_after": []}]
            draft = {"gap": 1, "speaker": "riffer", "line": "draft",
                     "words": 1, "when": 0.0}
            rewrite = {"gap": 1, "speaker": "riffer", "line": "final",
                       "words": 1, "when": 0.0}
            with patch.object(writer, "_write_drafts",
                              side_effect=lambda *args, **kwargs:
                              [rewrite] if kwargs.get("critique_context") else [draft]), \
                 patch("mst3k.judge.judge_riffs", return_value=[
                     {"gap": 1, "verdict": "rewrite", "critique": "be sharper", "score": 2}
                 ]):
                result = writer.write_riffs_with_review(job, gaps, {}, bundles)
            self.assertEqual(result[0]["line"], "final")
            self.assertFalse((Path(tmp) / "riffs.json").exists())
            self.assertEqual(json.loads((Path(tmp) / "judged_riffs.json").read_text())[0]["line"], "final")

    def test_asr_oom_error_is_actionable(self):
        exc = subprocess.CalledProcessError(-9, ["asr"])
        message = str(transcribe._chunk_error(exc, 3, 14))
        self.assertIn("chunk 3/14", message)
        self.assertIn("likely ran out of memory", message)

    def test_no_audio_transcription_is_a_valid_empty_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = transcribe.transcribe({"dir": Path(tmp), "meta": {"has_audio": False}})
            self.assertEqual(result["lines"], [])
            self.assertTrue((Path(tmp) / "transcript_policy.json").exists())

    def test_short_audio_has_no_negative_moment_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = dict(config.DEFAULTS)
            job.update({"dir": Path(tmp), "meta": {"duration": 1.0}})
            with patch.object(analyze, "_audio_windows", return_value=[]):
                self.assertEqual(analyze._detect_quiet_moments(Path("missing.wav"), job), [])

    def test_density_is_a_real_baseline_and_extra_dense_scales_it(self):
        base = {"meta": {"duration": 600.0}, "kind": "movie", "max_riffs": 400,
                "riff_pace_per_kind": config.DEFAULTS["riff_pace_per_kind"]}
        counts = [analyze.target_riff_count({**base, "riff_density_bias": bias})
                  for bias in range(5)]
        self.assertEqual(counts, sorted(counts))
        self.assertGreater(counts[2], 15)
        self.assertGreater(counts[4], counts[2])

    def test_llm_repairs_truncated_json_once(self):
        client = llm.LLM("https://example.invalid", "key", "model")
        with patch.object(client, "chat", side_effect=['{"x":', '{"x": 1}']):
            self.assertEqual(client.chat_json([{"role": "user", "content": "json"}])["x"], 1)

    def test_llm_retries_an_empty_gateway_content_response(self):
        client = llm.LLM("https://openrouter.ai/api/v1", "key", "z-ai/glm-5.3-flash")
        with patch.object(client, "chat", side_effect=[
            RuntimeError("LLM response from ... contained no text content"),
            '{"ok": true}'
        ]):
            self.assertTrue(client.chat_json([{"role": "user", "content": "json"}])["ok"])

        with tempfile.TemporaryDirectory() as tmp:
            job = dict(config.DEFAULTS)
            job.update({"dir": Path(tmp), "source": Path("missing.mp4"),
                        "meta": {"duration": 120.0}, "kind": "movie",
                        "riff_density_bias": 2})
            with patch.object(analyze, "extract_audio", return_value=Path(tmp) / "audio.wav"), \
                 patch.object(analyze, "_detect_silence", return_value=[]), \
                 patch.object(analyze, "_audio_windows", return_value=[]), \
                 patch.object(analyze, "find_cuts", return_value=[]):
                cues = analyze.find_gaps(job)
            self.assertEqual(len(cues), job["target_riff_count"])
            self.assertTrue(all(cue["kind"] == "cadence" for cue in cues))

    def test_writer_context_stops_at_the_cue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.json"
            path.write_text(json.dumps({"lines": [
                {"start": 1.0, "end": 2.0, "text": "already happened"},
                {"start": 5.0, "end": 6.0, "text": "future reveal"},
            ]}))
            context = writer._full_transcript({"dir": Path(tmp)}, [], through=4.0)
            self.assertIn("already happened", context)
            self.assertNotIn("future reveal", context)

        gaps = [{"id": 1, "start": 10.0, "end": 11.0, "budget_words": 2}]
        job = {"max_line_chars": 240}
        result = writer._normalize_response([{
            "gap": 1, "line": "This is a longer deliberate aside.",
            "when": -0.4, "timing": "overlap", "mechanism": "comparison"
        }], gaps, [], job)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timing"], "overlap")

    def test_voice_does_not_drop_a_long_riff_for_preferred_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tts = Path(tmp) / "tts"
            tts.mkdir()
            key = voice._cache_key("a long aside", "")
            wav = tts / f"{key}.wav"
            wav.write_bytes(b"placeholder")
            job = {"dir": Path(tmp), "voices": [{"name": None, "pitch": 0}],
                   "voice_pitch": 0, "voice_rate": 1, "voice_ref": None,
                   "pocket_tts": "pocket-tts", "max_riff_seconds": 9,
                   "max_tempo_stretch": 1.18}
            riff = {"gap": 1, "line": "a long aside", "_gap": {"usable": .8}}
            with patch.object(voice, "probe_duration", return_value=2.5):
                result = voice.synthesize(job, riff)
            self.assertTrue(result["ok"])
            self.assertTrue(result["overlaps_dialogue"])


if __name__ == "__main__":
    unittest.main()
