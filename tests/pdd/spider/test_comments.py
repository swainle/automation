import sys
import tempfile
import unittest
import csv
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.spider import comments


class CommentsTests(unittest.TestCase):
    def test_batch_filters_old_responses_deduplicates_and_limits(self):
        page = Mock()
        callbacks = {}
        page.on.side_effect = lambda event, fn: callbacks.update({event: fn})
        def trigger():
            old = Mock(url="https://example.com/proxy/api/reviews/123/list")
            late = Mock(request=old, url=old.url)
            late.json.return_value = {"data": [{"review_id": "wrong"}]}
            callbacks["response"](late)
            request = Mock(url=old.url)
            callbacks["request"](request)
            response = Mock(request=request, url=request.url)
            response.json.return_value = {"data": [{"review_id": str(i)} for i in range(120)]
                                          + [{"review_id": "1"}]}
            callbacks["response"](response)
        result = comments._collect_review_batch(page, trigger, 100)
        self.assertEqual([r["review_id"] for r in result], [str(i) for i in range(100)])
        self.assertEqual(page.remove_listener.call_count, 2)

    def test_listener_cleanup_on_click_failure(self):
        page = Mock()
        with self.assertRaises(RuntimeError):
            comments._collect_review_batch(page, Mock(side_effect=RuntimeError("click")), 100)
        self.assertEqual(page.remove_listener.call_count, 2)

    def test_negative_tags_combined_deduplicated_with_same_columns(self):
        page = Mock()
        page.locator.return_value.count.return_value = 1
        normal = [{"review_id": "normal", "comment": "普通评价"}]
        first = [{"review_id": str(i), "comment": "差评"} for i in range(70)]
        second = [{"review_id": str(i), "comment": "差评"} for i in range(50, 130)]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(comments, "_collect_review_batch", side_effect=[normal, first, second]), \
                patch.object(comments, "_negative_tag_names", return_value=["包装差", "物流慢"]):
            comments.scrape_comments(page, Path(tmp))
            with (Path(tmp) / "差评.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], comments.COMMENT_HEADER + ["原因"])
            self.assertEqual(len(rows), 101)
            self.assertTrue(all(len(r) == 9 for r in rows))
            self.assertEqual(rows[1][-1], "包装差")
            with (Path(tmp) / "评论.csv").open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual(len(list(csv.reader(f))), 2)

    def test_no_gray_tags_writes_no_csv_and_records_reason(self):
        page = Mock()
        page.locator.return_value.count.return_value = 1
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(comments, "_collect_review_batch", return_value=[]), \
                patch.object(comments, "_negative_tag_names", return_value=[]):
            comments.scrape_comments(page, Path(tmp))
            self.assertFalse((Path(tmp) / "差评.csv").exists())
            from automation.pdd.spider.status import load_status
            state = load_status(Path(tmp))["stages"]["negative_comments"]
            self.assertEqual(state["state"], "skipped")
            self.assertEqual(state["reason"], "无灰色负面标签")

    def test_force_normal_does_not_collect_negative_and_reexecutes(self):
        from automation.pdd.spider.status import update_stage
        page = Mock()
        page.locator.return_value.count.return_value = 1
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(comments, "_collect_review_batch", return_value=[]) as batch, \
                patch.object(comments, "_negative_tag_names") as tags:
            update_stage(Path(tmp), "comments", "completed")
            comments.scrape_comments(page, Path(tmp), normal=True, negative=False, force=True)
            batch.assert_called_once()
            tags.assert_not_called()

    def test_negative_only_does_not_write_normal_comments(self):
        page = Mock()
        page.locator.return_value.count.return_value = 1
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(comments, "_collect_review_batch", return_value=[{"review_id": "1"}]) as batch, \
                patch.object(comments, "_negative_tag_names", return_value=["包装差"]):
            comments.scrape_comments(page, Path(tmp), normal=False, negative=True, force=True)
            batch.assert_called_once()
            self.assertFalse((Path(tmp) / "评论.csv").exists())
            self.assertTrue((Path(tmp) / "差评.csv").exists())


if __name__ == "__main__":
    unittest.main()
