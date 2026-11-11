import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from automation.pdd.spider.collectors import IosElementRanking, RankingItem
from automation.pdd.spider.crop import Banner, banner_rectangle, card_rectangle, compose_product, elements_from_xml
from automation.pdd.spider.runtime import Runtime


def screen(header=120):
    image = Image.new("RGB", (200, 600), (250, 248, 242))
    ImageDraw.Draw(image).rectangle((0, 0, 199, header - 1), fill=(30, 15, 5))
    return image


def source(title="目标商品", badge_y=140):
    return (f'<XCUIElementTypeApplication x="0" y="0" width="100" height="300">'
            f'<XCUIElementTypeOther x="1" y="{badge_y}" width="10" height="9" visible="false"/>'
            f'<XCUIElementTypeStaticText label="{title}" x="40" y="{badge_y + 15}" width="50" height="15"/>'
            '</XCUIElementTypeApplication>')


class CropTests(unittest.TestCase):
    def test_banner_has_variable_height_and_card_uses_element_and_ten_extra_pixels(self):
        for height in (80, 120):
            image = screen(height)
            self.assertEqual(banner_rectangle(image), (0, 0, 200, height))
            box, _ = card_rectangle(image.size, elements_from_xml(source()), "目标商品")
            self.assertEqual(box, (0, 280, 200, 390))
            result, report = compose_product(image, elements_from_xml(source()), "目标商品", Banner.from_image(image))
            self.assertEqual(result.size, (200, height + 110))
            self.assertEqual(report["bottom_extra_pixels"], 10)

    def test_wrong_title_occluded_and_out_of_frame_cards_are_rejected(self):
        image = screen()
        banner = Banner.from_image(image)
        with self.assertRaisesRegex(ValueError, "唯一匹配"):
            card_rectangle(image.size, elements_from_xml(source()), "其他商品")
        with self.assertRaisesRegex(ValueError, "超出截图"):
            card_rectangle(image.size, elements_from_xml(source(badge_y=260)), "目标商品")
        with self.assertRaisesRegex(ValueError, "遮挡"):
            compose_product(image, elements_from_xml(source(badge_y=30)), "目标商品", banner)

    def test_missing_screen_geometry_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "屏幕尺寸"):
            card_rectangle((200, 600), [], "目标商品")

    def test_collector_reuses_saved_screenshot_without_an_extra_capture(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            item = RankingItem("主榜", directory=path, row={"排行": 2, "标题": "目标商品"})
            runtime = Runtime()
            client = Mock()
            client.screenshot.return_value = screen()
            collector = IosElementRanking(runtime)
            collector.workflow.set_items([item], item_conditions={"rank_item", "rank_context"})
            with patch.object(runtime, "ensure_wda", return_value=client), \
                    patch("automation.pdd.spider.ios._current_page_source", return_value=source()) as xml:
                collector.run("[get_screenshot,get_crop_image]")
            client.screenshot.assert_called_once()
            xml.assert_called_once()
            with Image.open(item.image_directory / "产品截图.jpg") as result:
                self.assertEqual(result.width, 200)
                self.assertGreaterEqual(result.height, 228)
            self.assertTrue((item.image_directory / "排行截图.meta.json").is_file())
            report = json.loads((item.image_directory / "产品截图.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(report["card_box"], [0, 280, 200, 390])
            previous = (item.image_directory / "产品截图.jpg").read_bytes()
            Image.new("RGB", (200, 600), "red").save(item.image_directory / "排行截图.jpg")
            with self.assertRaisesRegex(ValueError, "不匹配"):
                collector.get_crop_image(item)
            self.assertEqual((item.image_directory / "产品截图.jpg").read_bytes(), previous)

    def test_subrank_reference_banner_is_reused_after_header_collapses(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            rank = RankingItem("主榜", directory=path)
            items = [RankingItem("主榜", directory=path, row={"排行": i, "标题": f"商品{i}"})
                     for i in (1, 2)]
            runtime = Runtime()
            client = Mock()
            client.screenshot.side_effect = [screen(120), screen(40), screen(40)]
            collector = IosElementRanking(runtime)
            collector.workflow.set_items([rank], item_conditions={"tab_item", "rank_context"})
            with patch.object(runtime, "ensure_wda", return_value=client), \
                    patch.object(collector, "list_rank_data", side_effect=lambda item: collector.workflow.set_items(items)), \
                    patch("automation.pdd.spider.ios._current_page_source", side_effect=[source(), source("商品1"), source("商品2")]):
                collector.run("[list_rank_data,get_screenshot,[get_screenshot,get_crop_image]]")
            self.assertEqual(client.screenshot.call_count, 3)
            with Image.open(path / "榜单截图.jpg") as reference:
                height = Banner.from_image(reference).height
            for item in items:
                with Image.open(item.image_directory / "产品截图.jpg") as result:
                    self.assertEqual(result.size, (200, height + 110))

    def test_banner_cache_is_isolated_between_subranks(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            ranks = [RankingItem("主榜", directory=path / name) for name in ("子榜A", "子榜B")]
            runtime = Runtime()
            client = Mock()
            client.screenshot.side_effect = [screen(120), screen(40), screen(80), screen(40)]
            collector = IosElementRanking(runtime)
            collector.workflow.set_items(ranks, item_conditions={"tab_item", "rank_context"})

            def initialize(rank):
                collector.workflow.set_items([RankingItem("主榜", directory=rank.directory,
                    row={"排行": 1, "标题": rank.directory.name})])

            with patch.object(runtime, "ensure_wda", return_value=client), \
                    patch.object(collector, "list_rank_data", side_effect=initialize), \
                    patch("automation.pdd.spider.ios._current_page_source", side_effect=[
                        source("子榜A"), source("子榜A"), source("子榜B"), source("子榜B")]):
                collector.run("[list_rank_data,get_screenshot,[get_screenshot,get_crop_image]]")
            heights = []
            for rank in ranks:
                with Image.open(rank.directory / "榜单截图.jpg") as reference:
                    expected = Banner.from_image(reference).height + 110
                with Image.open(rank.directory / "榜01" / "产品截图.jpg") as result:
                    self.assertEqual(result.height, expected)
                    heights.append(result.height)
            self.assertNotEqual(*heights)


if __name__ == "__main__":
    unittest.main()
