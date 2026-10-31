from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from PIL import Image, ImageDraw

from automation.pdd.captcha.yidun import analyze_gap


def geometry():
    return {"geometry": {"background": {"x": 40, "y": 40, "width": 320, "height": 160},
        "piece": {"x": 40, "y": 70, "width": 40, "height": 50},
        "button": {"x": 40, "y": 220, "width": 40, "height": 40},
        "track": {"x": 40, "y": 220, "width": 320, "height": 40}}, "alpha_bounds": None}


class YidunAnalysisTests(unittest.TestCase):
    def test_scaled_image_distance_is_css_pixels(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "background.png"
            image = Image.new("RGB", (640, 320), "#dddddd")
            ImageDraw.Draw(image).rectangle((360, 60, 439, 159), fill="#222222")
            image.save(path)
            result = analyze_gap(path, geometry(), folder)
            self.assertEqual(result["status"], "recognized")
            self.assertEqual(result["gap_x"], 360)
            self.assertEqual(result["distance"], 180)

    def test_equal_edges_stop_as_ambiguous(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "background.png"
            image = Image.new("RGB", (320, 160), "#dddddd")
            draw = ImageDraw.Draw(image)
            for left in [100, 220]: draw.rectangle((left, 30, left+39, 79), fill="#222222")
            image.save(path)
            self.assertEqual(analyze_gap(path, geometry(), folder)["status"], "ambiguous")

    def test_uniform_image_is_not_a_gap(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "background.png"
            Image.new("RGB", (320, 160), "#dddddd").save(path)
            self.assertEqual(analyze_gap(path, geometry(), folder)["status"], "weak_edge")
