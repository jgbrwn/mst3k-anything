import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mst3k import analyze, writer
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

    def test_short_audio_has_no_negative_moment_window(self):
        job = {"meta": {"duration": 1.0}, "moment_win_sec": 1.6,
               "moment_hop_sec": 1.2, "lead_in_sec": 3.0}
        self.assertEqual(analyze._detect_quiet_moments(Path("/does/not/exist"), job), [])


if __name__ == "__main__":
    unittest.main()
