"""Cookie 文件 HTTP 接口服务；仅本机访问，不鉴权，自带 Swagger 文档。"""

import io
import json
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_restx import Api, Resource, fields
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import HTTPException

from automation.com.cookies_server.config import ServerConfig
from automation.com.cookies_server.storage import (
    StorageError,
    list_files,
    read_bytes,
    resolve_under,
    safe_filename,
    validate_cookies_data,
    write_bytes,
)

from automation.config import ROOT

_HTTP_MESSAGES = {
    404: "接口不存在",
    405: "方法不允许",
    500: "内部错误",
}


def create_app(cookies_dir: Path) -> Flask:
    """创建 Flask 应用；数据目录通过参数注入以便测试。"""
    root = Path(cookies_dir).resolve()
    app = Flask(__name__)
    app.config["ERROR_INCLUDE_MESSAGE"] = False
    app.config["RESTX_ERROR_404_HELP"] = False

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    api = Api(
        app,
        title="Cookie 文件服务",
        version="1.0",
        description="Cookie 文件的列表、下载与上传接口；仅本机访问，无鉴权。",
        doc="/docs",
    )

    @api.errorhandler(HTTPException)
    def handle_http_error(error):
        code = error.code or 500
        return {"error": _HTTP_MESSAGES.get(code, "请求错误")}, code

    file_model = api.model("CookieFile", {
        "name": fields.String(description="相对数据目录的文件路径（含 .json）"),
        "size": fields.Integer(description="文件字节数"),
        "modified": fields.String(description="修改时间（ISO 字符串）"),
    })
    upload_model = api.model("UploadResult", {
        "name": fields.String(description="相对数据目录的文件路径（含 .json）"),
        "size": fields.Integer(description="文件字节数"),
    })
    error_model = api.model("Error", {"error": fields.String(description="错误信息")})

    list_parser = api.parser()
    list_parser.add_argument("dir", location="args", required=False, help="目标子目录（可选，默认顶层）")

    upload_parser = api.parser()
    upload_parser.add_argument("file", location="files", type=FileStorage, required=True, help="Cookie JSON 文件")
    upload_parser.add_argument("dir", location="form", required=False, help="目标子目录（可选，默认顶层）")
    upload_parser.add_argument("name", location="form", required=False, help="覆盖存储文件名（可选，默认用上传文件原名）")

    @api.route("/cookies")
    class CookieCollection(Resource):
        @api.doc(description="列出 Cookie 文件（不含 value），不递归。")
        @api.expect(list_parser)
        @api.response(200, "成功", [file_model])
        @api.response(404, "目录不存在", error_model)
        def get(self):
            try:
                dir_value = (request.args.get("dir") or "").strip()
                directory = resolve_under(root, dir_value) if dir_value else root
                return list_files(directory, root)
            except StorageError as exc:
                return {"error": exc.message}, exc.status

        @api.doc(description="上传 Cookie JSON 文件（multipart），同名覆盖。")
        @api.expect(upload_parser)
        @api.response(201, "上传成功", upload_model)
        @api.response(400, "请求非法", error_model)
        def post(self):
            try:
                file = request.files.get("file")
                if file is None:
                    raise StorageError(400, "缺少 file 字段")
                raw = file.read()
                try:
                    obj = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise StorageError(400, "文件不是合法 JSON")
                validate_cookies_data(obj)

                dir_value = (request.form.get("dir") or "").strip()
                directory = resolve_under(root, dir_value) if dir_value else root
                name = safe_filename((request.form.get("name") or file.filename or "").strip())
                target = (directory / name).resolve()
                if target != root and root not in target.parents:
                    raise StorageError(400, "路径越界")
                write_bytes(target, raw)
                return {
                    "name": str(target.relative_to(root)).replace("\\", "/"),
                    "size": len(raw),
                }, 201
            except StorageError as exc:
                return {"error": exc.message}, exc.status

    @api.route("/cookies/<path:name>")
    class CookieFile(Resource):
        @api.doc(description="下载原始 Cookie JSON 文件（含 value）。")
        @api.response(200, "成功，返回 JSON 文件")
        @api.response(404, "文件不存在", error_model)
        def get(self, name):
            try:
                target = resolve_under(root, name)
                if target.suffix.lower() != ".json":
                    raise StorageError(404, "文件不存在")
                data = read_bytes(target)
                return send_file(
                    io.BytesIO(data),
                    mimetype="application/json",
                    as_attachment=True,
                    download_name=target.name,
                )
            except StorageError as exc:
                return {"error": exc.message}, exc.status

    @app.errorhandler(404)
    def handle_not_found(_error):
        return jsonify({"error": "接口不存在"}), 404

    @app.errorhandler(500)
    def handle_internal_error(_error):
        return jsonify({"error": "内部错误"}), 500

    return app


def main(*, root=ROOT) -> int:
    try:
        config = ServerConfig.load(root)
        app = create_app(config.cookies_dir)
        print(
            f"cookies_server 已启动：http://{config.host}:{config.port}/（数据目录 {config.cookies_dir}）",
            flush=True,
        )
        print(f"Swagger 文档：http://{config.host}:{config.port}/docs", flush=True)
        app.run(host=config.host, port=config.port, debug=False)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"启动失败（{type(exc).__name__}）：{exc}", flush=True)
        return 1
