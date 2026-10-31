"""cookies_server HTTP 接口测试，使用 Flask test_client，不真实起服务。"""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from automation.com.cookies_server.app import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.app = create_app(self.root)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_empty(self):
        resp = self.client.get("/cookies")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_upload_download_roundtrip(self):
        content = {"url": "https://mms.pinduoduo.com/home", "cookies": [{"name": "a", "value": "b"}]}
        resp = self.client.post(
            "/cookies",
            data={"file": (io.BytesIO(json.dumps(content).encode("utf-8")), "一往无前宠物用品店cookie.json")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["name"], "一往无前宠物用品店cookie.json")

        resp = self.client.get("/cookies/一往无前宠物用品店cookie.json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.get_data(as_text=True)), content)

    def test_upload_to_subdir_and_list(self):
        content = {"cookies": [{"name": "a", "value": "b"}]}
        resp = self.client.post(
            "/cookies",
            data={"file": (io.BytesIO(json.dumps(content).encode("utf-8")), "x.json"), "dir": "黄金万两"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["name"], "黄金万两/x.json")

        resp = self.client.get("/cookies")
        self.assertEqual(resp.get_json(), [])
        resp = self.client.get("/cookies?dir=黄金万两")
        self.assertEqual([item["name"] for item in resp.get_json()], ["黄金万两/x.json"])

    def test_upload_overrides_name(self):
        content = {"cookies": [{"name": "a", "value": "b"}]}
        resp = self.client.post(
            "/cookies",
            data={"file": (io.BytesIO(json.dumps(content).encode("utf-8")), "orig.json"), "name": "重命名.json"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["name"], "重命名.json")

    def test_upload_rejects_invalid_json(self):
        resp = self.client.post(
            "/cookies",
            data={"file": (io.BytesIO(b"not json"), "x.json")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_upload_rejects_invalid_structure(self):
        resp = self.client.post(
            "/cookies",
            data={"file": (io.BytesIO(json.dumps({"cookies": []}).encode("utf-8")), "x.json")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_missing_file(self):
        resp = self.client.post("/cookies", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)

    def test_download_missing(self):
        resp = self.client.get("/cookies/nope.json")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "文件不存在"})

    def test_download_rejects_traversal(self):
        resp = self.client.get("/cookies/..%2Fsecret.json")
        self.assertIn(resp.status_code, (400, 404))

    def test_method_not_allowed_returns_json(self):
        resp = self.client.put("/cookies")
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(resp.get_json(), {"error": "方法不允许"})


if __name__ == "__main__":
    unittest.main()
