from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from automation.pdd.captcha.candidates import analyze_candidates


class CandidateAnalysisTests(unittest.TestCase):
    def test_two_separate_dark_shapes_are_compared_and_annotated(self):
        import numpy as np
        from PIL import Image

        rng = np.random.default_rng(12)
        background = np.full((60, 140, 3), 210, dtype=np.uint8)
        piece = np.zeros((60, 26, 4), dtype=np.uint8)
        mask = np.ones((20, 20), dtype=bool)
        mask[:5, :5] = False
        texture = rng.integers(140, 220, (20, 20, 3), dtype=np.uint8)
        piece[15:35, 2:22, :3] = texture
        piece[15:35, 2:22, 3] = mask * 255
        for x in (60, 105):
            region = background[15:35, x:x+20]
            region[mask] = (texture[mask] * 0.2).astype(np.uint8)
        buffers = []
        for array in (background, piece):
            stream = BytesIO()
            Image.fromarray(array).save(stream, format="PNG")
            buffers.append(stream.getvalue())
        with TemporaryDirectory() as directory:
            output = Path(directory) / "candidates.png"
            result = analyze_candidates(*buffers, output)
            self.assertTrue(output.is_file())
            self.assertEqual(sorted(c["x"] for c in result["candidates"]), [60, 105])
            self.assertIsNone(result["recommended_rank"])
            self.assertTrue(all(0 <= c["score"] <= 1 for c in result["candidates"]))

    def test_opaque_piece_has_no_usable_alpha_outline(self):
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (40, 40), "gray").save(buffer, format="PNG")
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                analyze_candidates(buffer.getvalue(), buffer.getvalue(), Path(directory) / "unused.png")


if __name__ == "__main__":
    unittest.main()
