"""内置 mitm 工作进程及抓包插件，由 Runtime 按需启动。"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import socket
import sys
import zlib
from datetime import datetime

from mitmproxy import http

# Windows 控制台统一 UTF-8，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------- 配置
# 项目根目录（src/spider/ 往上两级），输出统一落到 <项目根>/data/grab/mitm
from automation.pdd.spider.config import BUSINESS_ROOT

PROJECT_ROOT = BUSINESS_ROOT
OUTPUT_ROOT = PROJECT_ROOT / "data" / "grab" / "mitm"
MITM_CONFIG_FILE = PROJECT_ROOT / "data" / "mitm_config.json"
PDD_HOST_SUFFIXES = ("yangkeduo.com", "pinduoduo.com")
INTERESTING_KEYWORDS = (
    "rank", "list", "board", "leaderboard", "operating", "goods",
    "top", "tab", "scene", "search", "api",
)
RANK_HINT_KEYWORDS = ("rank", "board", "leaderboard", "operating", "goods_list", "top")
MAX_REQ_BODY = 64 * 1024


# ---------------------------------------------------------------- 工具函数
def _safe_name(text: str, limit: int = 70) -> str:
    s = re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("_") or "root"
    return s[:limit]


def _decode_body(raw: bytes | None, content_encoding: str | None) -> bytes:
    if not raw:
        return b""
    enc = (content_encoding or "").lower().strip()
    try:
        if not enc or enc == "identity":
            return raw
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "br" in enc:
            import brotli
            return brotli.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if "zstd" in enc:
            try:
                import zstandard
                return zstandard.ZstdDecompressor().decompress(raw)
            except ImportError:
                pass
    except Exception:
        pass
    return raw


def _body_bytes(msg) -> bytes:
    try:
        content = msg.content  # mitmproxy: 已解压
        if content is not None:
            return content
    except Exception:
        pass
    return _decode_body(msg.raw_content, msg.headers.get("content-encoding"))


def _pretty_json(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        return text


def _is_text(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return (
        "json" in ct or "text" in ct or "html" in ct or "xml" in ct
        or "javascript" in ct or "x-www-form-urlencoded" in ct or "form" in ct
    )


def _is_json(content_type: str) -> bool:
    return "json" in (content_type or "").lower()


# ---------------------------------------------------------------- 抓包插件
class PddCapture:
    def __init__(self):
        self.session = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out = OUTPUT_ROOT / self.session
        self.interesting_dir = self.out / "interesting"
        self.interesting_dir.mkdir(parents=True, exist_ok=True)

        self.index_path = self.out / "all_requests.csv"
        self._fh = open(self.index_path, "w", newline="", encoding="utf-8-sig")
        self._csv = csv.writer(self._fh)
        self._csv.writerow([
            "seq", "time", "method", "host", "path", "status",
            "content_type", "size", "saved",
        ])
        self._seq = 0
        self._saved = 0
        self._errors = 0
        print(f"[抓包] 输出目录: {self.out.resolve()}", flush=True)
        print("[抓包] 等待手机流量… 请在手机浏览器打开拼多多并进入榜单页", flush=True)

    # ----- 事件 -----
    def response(self, flow: http.HTTPFlow) -> None:
        self._seq += 1
        req = flow.request
        resp = flow.response
        ct = resp.headers.get("content-type", "") if resp else ""
        body = _body_bytes(resp) if resp else b""
        status = resp.status_code if resp else None

        host = req.host or ""
        path = req.path or "/"
        url_l = req.pretty_url.lower()

        is_pdd = host.endswith(PDD_HOST_SUFFIXES)
        is_json = _is_json(ct)
        has_kw = any(k in url_l for k in INTERESTING_KEYWORDS)
        interesting = is_pdd or is_json or has_kw

        self._csv.writerow([
            self._seq, datetime.now().strftime("%H:%M:%S.%f")[:-3],
            req.method, host, path, status, ct, len(body),
            "Y" if interesting else "",
        ])

        if interesting:
            self._save_flow(flow, body, ct, is_pdd, is_json)
        self._print_flow(req, resp, ct, body, is_pdd, is_json)

    def error(self, flow: http.HTTPFlow) -> None:
        self._errors += 1
        msg = getattr(flow, "error", None)
        msg = getattr(msg, "msg", None) or str(msg)
        host = getattr(flow.request, "host", "") if flow.request else ""
        print(f"[抓包] ⚠ 连接错误 (第{self._errors}个) host={host} | {msg}", flush=True)

    def done(self) -> None:
        self._fh.close()
        print("\n[抓包] 会话结束", flush=True)
        print(f"[抓包] 索引: {self.index_path.resolve()}", flush=True)
        print(f"[抓包] 完整保存: {self._saved} 个请求 -> {self.interesting_dir.resolve()}", flush=True)
        print(f"[抓包] 连接错误: {self._errors} 个", flush=True)

    # ----- 打印 -----
    def _print_flow(self, req, resp, ct, body, is_pdd, is_json) -> None:
        status = resp.status_code if resp else "-"
        size = len(body)
        url_l = req.pretty_url.lower()
        rank_hint = any(k in url_l for k in RANK_HINT_KEYWORDS)

        tag = []
        if is_pdd:
            tag.append("PDD")
        if is_json:
            tag.append("JSON")
        if rank_hint:
            tag.append("★榜单候选")
        tag_s = ("[" + "+".join(tag) + "]") if tag else ""

        marker = ">>> " if rank_hint else "    "
        print(
            f"{marker}{tag_s} {req.method} {status} {ct[:28] or '-'} "
            f"{size}B  {req.pretty_url[:160]}",
            flush=True,
        )

    # ----- 保存 -----
    def _save_flow(self, flow, body, ct, is_pdd, is_json) -> None:
        req = flow.request
        resp = flow.response

        path_name = _safe_name(req.path)
        url_hash = hashlib.md5(req.pretty_url.encode("utf-8", "ignore")).hexdigest()[:6]
        dir_name = f"{self._seq:05d}__{req.method}__{_safe_name(req.host, 40)}__{path_name}__{url_hash}"
        d = self.interesting_dir / dir_name
        d.mkdir(parents=True, exist_ok=True)

        meta = {
            "seq": self._seq,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "url": req.pretty_url,
            "method": req.method,
            "scheme": req.scheme,
            "host": req.host,
            "port": req.port,
            "path": req.path,
            "request_headers": dict(req.headers.items()),
            "request_content_type": req.headers.get("content-type", ""),
            "status": resp.status_code if resp else None,
            "reason": resp.reason if resp else None,
            "response_headers": dict(resp.headers.items()) if resp else {},
            "response_content_type": ct,
            "response_size": len(body),
            "is_pdd": is_pdd,
            "is_json": is_json,
        }
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        req_lines = [f"{req.method} {req.pretty_url} HTTP/{req.http_version}"]
        for k, v in req.headers.items():
            req_lines.append(f"{k}: {v}")
        req_lines.append("")
        req_body = _body_bytes(req)
        if req_body:
            req_lines.append(f"# ---- request body ({len(req_body)} bytes) ----")
            if _is_text(req.headers.get("content-type", "")) and len(req_body) <= MAX_REQ_BODY:
                req_lines.append(req_body.decode("utf-8", errors="replace"))
            else:
                req_lines.append(f"<binary/oversized {len(req_body)} bytes, not dumped>")
        (d / "request.txt").write_text("\n".join(req_lines), encoding="utf-8")

        if not body:
            (d / "response.txt").write_text("(empty body)", encoding="utf-8")
        elif is_json:
            (d / "response.json").write_text(_pretty_json(body), encoding="utf-8")
        elif _is_text(ct):
            (d / "response.txt").write_text(body.decode("utf-8", errors="replace"), encoding="utf-8")
        else:
            (d / "response.bin").write_bytes(body)

        self._saved += 1


class DynamicUpstream:
    """从 data/mitm_config.json 动态读取上游代理，每个新连接前刷新（免重启切 IP）。"""

    def _cfg(self):
        try:
            return json.loads(MITM_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def http_connect(self, flow):
        """收到 CONNECT 时，动态设置上游代理地址。"""
        cfg = self._cfg()
        upstream = cfg.get("upstream")
        if not upstream:
            return
        try:
            from mitmproxy.net.server_spec import ServerSpec
            host, _, port = upstream.rpartition(":")
            if host and port.isdigit():
                flow.server_conn.via = ServerSpec((cfg.get("upstream_scheme", "http"), (host.strip("[]"), int(port))))
        except Exception:
            pass

    def http_connect_upstream(self, flow):
        """CONNECT 发往上游前，设置账密认证。"""
        cfg = self._cfg()
        auth = cfg.get("upstream_auth")
        if auth:
            import base64
            flow.request.headers["Proxy-Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()


# 被 mitmdump 通过 -s 加载时（__name__ != "__main__"），暴露 addon
if __name__ != "__main__":
    addons = [PddCapture(), DynamicUpstream()]


# ---------------------------------------------------------------- 启动逻辑
def _is_private_lan(ip: str) -> bool:
    """过滤回环/链路本地/虚拟网卡（WSL、Docker、VirtualBox 的 172.16/12）。"""
    if ip.startswith(("127.", "169.254.")):
        return False
    octets = ip.split(".")
    if len(octets) == 4:
        try:
            second = int(octets[1])
            if int(octets[0]) == 172 and 16 <= second <= 31:
                return False
        except ValueError:
            pass
    return True


def get_lan_ips() -> tuple[str | None, list[tuple[str, str]]]:
    """返回 (推荐IP, [(ip, 接口名), ...])。

    优先用 PowerShell 拿「IP + 接口名」，把 WiFi 网卡标为推荐；
    拿不到接口名时退回 socket 探测，推荐 = 默认出口网卡。
    """
    entries: list[tuple[str, str]] = []
    try:
        import subprocess
        ps_cmd = (
            "Get-NetIPAddress -AddressFamily IPv4 | "
            "Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
            if isinstance(data, dict):
                data = [data]
            for d in data:
                ip = str(d.get("IPAddress", ""))
                name = str(d.get("InterfaceAlias", ""))
                if _is_private_lan(ip):
                    entries.append((ip, name))
    except Exception:
        entries = []

    if not entries:  # 兜底：socket 探测（无接口名）
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_private_lan(ip):
                entries.append((ip, ""))

    # 去重（保持顺序）
    seen, uniq = set(), []
    for ip, name in entries:
        if ip in seen:
            continue
        seen.add(ip)
        uniq.append((ip, name))

    # 推荐：优先 WiFi 网卡
    primary = None
    for ip, name in uniq:
        if re.search(r"wlan|wi-?fi|wireless|无线", name, re.I):
            primary = ip
            break
    if primary is None:  # 退回默认出口网卡
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            s.close()
            if not _is_private_lan(primary):
                primary = None
        except Exception:
            primary = None
    return primary, uniq


def print_guide(port: int) -> None:
    primary, entries = get_lan_ips()
    print("=" * 60)
    print("  拼多多榜单抓包监听")
    print("=" * 60)
    print(f"  监听端口: {port}")
    print()
    print("【1】手机与电脑连同一 WiFi，WiFi 代理设为「手动」：")
    print("      主机(服务器) 填下面带「推荐」的 IP（通常是 WLAN/WiFi 那个）：")
    for ip, name in entries:
        mark = "  <-- 推荐" if ip == primary else ""
        label = f"  ({name})" if name else ""
        print(f"        - {ip}{label}{mark}")
    print(f"      端口: {port}")
    print()
    print("【2】手机浏览器访问 http://mitm.it 下载并安装证书：")
    print("      Android: 下载 Android 证书 -> 安装（部分机型还需在「加密与凭据」信任）")
    print("      iPhone : 下载 Apple 证书 -> 描述文件安装 -> 通用-关于本机-证书信任设置 打开完全信任")
    print("      证书文件也在: ~\\.mitmproxy\\mitmproxy-ca-cert.cer")
    print()
    print("【3】手机浏览器打开拼多多 -> 跳转榜单 -> 打开榜单页，即可实时看到接口")
    print("      (拼多多 App 有证书固定抓不到，务必用浏览器网页版)")
    print()
    print("  抓完后运行分析: uv run tools\\mitm\\analyze.py")
    print("=" * 60)
    print()


def _save_rotation(rotation: str) -> None:
    """把切换 IP 的 API 持久化到 data/mitm_config.json，供 rank 采集风控时读取。"""
    cfg = {}
    try:
        cfg = json.loads(MITM_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    cfg["rotation"] = rotation
    MITM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    MITM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    print(f"[监听] 切换 IP API: {rotation}")


def _fetch_qg_ip(key: str, password: str | None = None) -> str | None:
    """通过青果 key 提取一个短效 IP，返回 server（ip:port）。"""
    import urllib.request
    url = f"https://share.proxy.qg.net/get?key={key}"
    if password:
        url += f"&pwd={password}"
    try:
        resp = urllib.request.urlopen(url, timeout=15).read()
        data = json.loads(resp.decode("utf-8"))
        if data.get("code") == "SUCCESS" and data.get("data"):
            item = data["data"][0]
            server = item.get("server")
            print(f"[提取IP] {item.get('proxy_ip')} -> {server} "
                  f"({item.get('area')} {item.get('isp')} 至 {item.get('deadline')})")
            return server
        print(f"[提取IP] 失败: {data.get('code')}")
    except Exception as ex:
        print(f"[提取IP] 异常: {ex}")
    return None


def main(port: int = 8080, upstream: str | None = None, rotation: str | None = None,
         key: str | None = None, password: str | None = None) -> None:
    print_guide(port)
    if rotation:
        _save_rotation(rotation)

    # 代理配置：
    # - 指定了 key/upstream：提取/设置上游代理并写回配置
    # - 都没指定（含只传 password 这种无意义情况）：清空上游，默认直连，避免复用残留的失效代理
    cfg = {}
    try:
        cfg = json.loads(MITM_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    if key or upstream:
        upstream_auth = None
        if key and not upstream:
            cfg["upstream_scheme"] = "http"
            cfg["key"] = key
            if password:
                cfg["password"] = password
            server = _fetch_qg_ip(key, password)
            if server:
                upstream = server  # ip:port
                if password:
                    upstream_auth = f"{key}:{password}"
        elif upstream:
            # 拆分 --upstream 里内嵌的账密（http://user:pass@host:port）
            import urllib.parse
            p = urllib.parse.urlsplit(upstream)
            if p.username:
                upstream_auth = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
            host = f"[{p.hostname}]" if p.hostname and ":" in p.hostname else p.hostname
            upstream = f"{host}:{p.port}" if p.port else host
            cfg["upstream_scheme"] = p.scheme or "http"

        if upstream:
            cfg["upstream"] = upstream
            if upstream_auth:
                cfg["upstream_auth"] = upstream_auth
            else:
                cfg.pop("upstream_auth", None)
        else:
            cfg.pop("upstream", None)
            cfg.pop("upstream_auth", None)
    else:
        # 未指定 key/upstream → 默认直连
        cfg.pop("upstream", None)
        cfg.pop("upstream_auth", None)

    MITM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    MITM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    # 打印当前生效的上游
    try:
        cur = json.loads(MITM_CONFIG_FILE.read_text(encoding="utf-8"))
        u = cur.get("upstream")
        if u:
            print(f"[监听] 上游代理: {u}"
                  + (" (已配置认证)" if cur.get("upstream_auth") else ""))
        else:
            print("[监听] 直连（未使用上游代理）")
    except Exception:
        pass

    from mitmproxy.tools import main as mitm_main

    args = [
        "-s", __file__,
        "--listen-host", "0.0.0.0",
        "--listen-port", str(port),
        "--set", "console_eventlog_verbosity=error",
        "--set", "connection_strategy=lazy",
    ]

    try:
        mitm_main.mitmdump(args)
    except KeyboardInterrupt:
        pass
    print("\n[监听] 已停止")


def run_cli() -> None:
    """命令行入口：解析 sys.argv 端口/上游代理/切换 API/key/password 参数后启动监听。"""
    port = 8080
    upstream = None
    rotation = None
    key = None
    password = None
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            return
        if a == "--upstream" and i + 1 < len(argv):
            upstream = argv[i + 1]
            i += 2
            continue
        if a == "--rotation" and i + 1 < len(argv):
            rotation = argv[i + 1]
            i += 2
            continue
        if a == "--key" and i + 1 < len(argv):
            key = argv[i + 1]
            i += 2
            continue
        if a == "--password" and i + 1 < len(argv):
            password = argv[i + 1]
            i += 2
            continue
        try:
            port = int(a)
        except ValueError:
            print(f"无效参数: {a}")
            return
        i += 1
    main(port, upstream, rotation, key, password)


if __name__ == "__main__":
    run_cli()
