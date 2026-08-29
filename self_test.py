#!/usr/bin/env python3
"""不访问真实智谱、不使用 API Key 的本地代理自检。"""

from __future__ import annotations

import argparse
import gzip
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from waitress.adjustments import Adjustments
from waitress.server import create_server

import bigmodel_diagnostic_proxy as proxy


CREATED = (
    b'event: response.created\n'
    b'data: {"type":"response.created","response":{"id":"resp_test","model":"glm-test"}}\n\n'
)
COMPLETED = (
    b'event: response.completed\n'
    b'data: {"type":"response.completed","response":{"id":"resp_test","model":"glm-test","status":"completed"}}\n\n'
)


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    captures: list[dict[str, Any]] = []
    capture_lock = threading.Lock()

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _capture(self, body: bytes) -> None:
        with self.capture_lock:
            self.captures.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": body,
                }
            )

    def _headers(
        self,
        status: int = 200,
        content_type: str = "text/event-stream; charset=utf-8",
        **headers: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.send_header("X-Log-Id", f"mock-{status}")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定名称
        self._handle(b"")

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定名称
        length = int(self.headers.get("Content-Length", "0") or "0")
        self._handle(self.rfile.read(length))

    def _handle(self, body: bytes) -> None:
        self._capture(body)
        query = parse_qs(urlsplit(self.path).query)
        scenario = query.get("scenario", ["normal"])[0]

        if scenario.startswith("status"):
            status = int(scenario.removeprefix("status"))
            payload = json.dumps(
                {
                    "error": {
                        "type": "mock_error",
                        "code": f"status_{status}",
                        "message": "测试错误；Authorization: Bearer SELF_TEST_LOG_SECRET",
                    }
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._headers(
                status,
                "application/json; charset=utf-8",
                Content_Length=str(len(payload)),
            )
            self.wfile.write(payload)
            return

        if scenario == "content_filter":
            payload = (
                b"<html><head><title>Content Filter - Access Denied</title></head>"
                b"<body><h1>504 Gateway Timeout</h1></body></html>"
            )
            self._headers(
                504,
                "text/html; charset=utf-8",
                Content_Length=str(len(payload)),
            )
            self.wfile.write(payload)
            return

        if scenario == "generic":
            payload = b'{"ok":true}'
            self._headers(200, "application/json", Content_Length=str(len(payload)))
            self.wfile.write(payload)
            return

        if scenario == "gzip":
            payload = gzip.compress(CREATED + COMPLETED)
            self._headers(
                200,
                Content_Encoding="gzip",
                Content_Length=str(len(payload)),
            )
            self.wfile.write(payload)
            return

        if scenario == "silent":
            self._headers()
            self.wfile.write(CREATED)
            self.wfile.flush()
            time.sleep(0.38)
            self.wfile.write(COMPLETED)
            self.wfile.flush()
            return

        if scenario == "partial":
            self._headers()
            first = (
                b'event: response.output_text.delta\n'
                b'data: {"type":"response.output_text.delta","delta":"par'
            )
            second = b'tial"}\n\n'
            self.wfile.write(first)
            self.wfile.flush()
            time.sleep(0.38)
            self.wfile.write(second + COMPLETED)
            self.wfile.flush()
            return

        if scenario == "early_eof":
            self._headers()
            self.wfile.write(CREATED)
            self.wfile.flush()
            return

        if scenario == "read_error":
            self._headers(Content_Length=str(len(CREATED) + 999))
            self.wfile.write(CREATED)
            self.wfile.flush()
            return

        if scenario == "hold":
            self._headers()
            self.wfile.write(CREATED)
            self.wfile.flush()
            for index in range(20):
                time.sleep(0.1)
                try:
                    self.wfile.write(f": upstream-hold-{index}\n\n".encode("ascii"))
                    self.wfile.flush()
                except OSError:
                    break
            return

        self._headers()
        self.wfile.write(CREATED + COMPLETED)
        self.wfile.flush()


class CheckFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return bool(predicate())


def run_self_test(keep_logs: bool = False) -> int:
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    temporary = tempfile.TemporaryDirectory(prefix="bigmodel-proxy-self-test-")
    log_dir = Path(temporary.name)
    settings = replace(
        proxy.Settings(),
        upstream_origin=f"http://127.0.0.1:{upstream.server_port}",
        log_dir=str(log_dir),
        startup_probe=False,
        trust_env_proxy=False,
        local_heartbeat_seconds=0.10,
        connect_timeout_seconds=2.0,
        read_timeout_seconds=3.0,
        write_timeout_seconds=2.0,
        pool_timeout_seconds=2.0,
    )
    diag = proxy.DiagnosticLogger(settings)
    # 自检结果保持简洁；完整中文事件仍写入临时日志并参与脱敏检查。
    if diag.logger.handlers:
        diag.logger.handlers[0].setLevel(logging.CRITICAL)
    app = proxy.create_app(settings, diag)
    local = create_server(
        app,
        host="127.0.0.1",
        port=0,
        threads=8,
        channel_timeout=120,
        cleanup_interval=1,
        clear_untrusted_proxy_headers=True,
    )
    local_thread = threading.Thread(target=local.run, daemon=True)
    local_thread.start()
    base = f"http://127.0.0.1:{local.effective_port}"
    results: list[tuple[str, bool, str]] = []

    def record(name: str, action) -> None:
        try:
            action()
            results.append((name, True, "通过"))
        except Exception as exc:  # 自检需要继续报告其他项目
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    request_body = json.dumps(
        {
            "model": "glm-test",
            "stream": True,
            "previous_response_id": "resp_previous",
            "input": "SELF_TEST_PROMPT_SECRET",
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer SELF_TEST_AUTH_SECRET",
        "Cookie": "session=SELF_TEST_COOKIE_SECRET",
    }

    def post(path: str) -> httpx.Response:
        return httpx.post(base + path, headers=headers, content=request_body, timeout=5.0)

    def normal_and_routes() -> None:
        for path in (
            "/api/v1/responses?scenario=normal",
            "/v1/responses?scenario=normal",
            "/responses?scenario=normal",
        ):
            response = post(path)
            check(response.status_code == 200, f"{path} 返回 {response.status_code}")
            check(b"response.completed" in response.content, f"{path} 缺少完成事件")
            check(b"response.failed" not in response.content, f"{path} 被误判为断流")

    record("动态路由和正常 completed 流", normal_and_routes)

    def forwarding() -> None:
        capture = MockUpstream.captures[-1]
        check(capture["body"] == request_body, "请求正文未透明转发")
        lower_headers = {k.lower(): v for k, v in capture["headers"].items()}
        check(
            lower_headers.get("authorization") == "Bearer SELF_TEST_AUTH_SECRET",
            "Authorization 未透明转发",
        )
        check(lower_headers.get("cookie") == "session=SELF_TEST_COOKIE_SECRET", "Cookie 未透明转发")
        check(lower_headers.get("accept-encoding") == "identity", "未强制 identity 编码")
        check(lower_headers.get("connection", "").lower() != "close", "NVIDIA 模式误发 Connection: close")
        check(settings.transport_profile == "nvidia", "自检没有使用 NVIDIA 兼容传输模式")
        check(settings.reuse_upstream_client is True, "NVIDIA 模式没有复用共享客户端")

    record("上下文正文和请求头透明转发", forwarding)

    def heartbeat() -> None:
        response = post("/api/v1/responses?scenario=silent")
        check(b"local-proxy-heartbeat" in response.content, "静默期间没有本地心跳")
        check(b"response.completed" in response.content, "静默恢复后未完成")

    record("完整事件之间的静默心跳", heartbeat)

    def partial_event() -> None:
        response = post("/api/v1/responses?scenario=partial")
        check(b'"delta":"partial"' in response.content, "半个 SSE 事件被心跳破坏")
        check(b"response.completed" in response.content, "半事件恢复后未完成")
        check(
            any(item["event"] == "上游静默且事件未闭合" for item in diag.tail(2000)),
            "未记录半事件静默诊断",
        )

    record("半个 SSE 事件静默时不破坏 JSON", partial_event)

    def early_eof() -> None:
        response = post("/api/v1/responses?scenario=early_eof")
        check(b"event: response.failed" in response.content, "提前 EOF 未合成失败事件")
        check(
            any(item["event"] == "上游提前断流" for item in diag.tail(2000)),
            "提前 EOF 未写中文日志",
        )

    record("提前 EOF", early_eof)

    def read_error() -> None:
        response = post("/api/v1/responses?scenario=read_error")
        check(b"event: response.failed" in response.content, "读取异常未合成失败事件")
        check(
            any(item["event"] == "上游读取异常" for item in diag.tail(2000)),
            "读取异常未写中文日志",
        )

    record("上游读取异常", read_error)

    def http_errors() -> None:
        for status in (401, 429, 503):
            response = post(f"/api/v1/responses?scenario=status{status}")
            check(response.status_code == status, f"HTTP {status} 被改为 {response.status_code}")
            check(response.json()["error"]["code"] == f"status_{status}", "错误正文被修改")

    record("401/429/503 原样返回", http_errors)

    def content_filter_classification() -> None:
        response = post("/api/v1/responses?scenario=content_filter")
        check(response.status_code == 504, "内容过滤器 504 状态未原样返回")
        check(b"Content Filter - Access Denied" in response.content, "内容过滤拒绝页被修改")
        check(
            any(item["event"] == "疑似企业代理内容过滤拒绝" for item in diag.tail(2000)),
            "没有生成企业内容过滤分类日志",
        )

    record("504 Content Filter 分类", content_filter_classification)

    def isolated_headers() -> None:
        isolated = replace(
            settings,
            transport_profile="isolated",
            connection_close=True,
            reuse_upstream_client=False,
            follow_redirects=False,
        )
        forwarded = proxy._forward_headers({"Accept": "text/event-stream"}.items(), isolated)
        check(forwarded.get("Connection") == "close", "isolated 模式没有关闭连接复用")

    record("isolated 每请求新连接模式", isolated_headers)

    def generic_and_gzip() -> None:
        generic = post("/api/v1/models?scenario=generic")
        check(generic.json() == {"ok": True}, "普通 JSON 响应转发失败")
        compressed = post("/api/v1/responses?scenario=gzip")
        check(b"response.completed" in compressed.content, "压缩 SSE 没有正确解压转发")
        check("content-encoding" not in compressed.headers, "解压后仍错误保留 Content-Encoding")

    record("普通响应与异常压缩 SSE", generic_and_gzip)

    def client_disconnect() -> None:
        client = proxy._http_client(settings)
        upstream_request = client.build_request(
            "POST",
            f"{settings.upstream_origin}/api/v1/responses?scenario=hold",
            headers={"Content-Type": "application/json", "Connection": "close"},
            content=request_body,
        )
        response = client.send(upstream_request, stream=True)
        relay = proxy._sse_relay(
            response,
            client,
            True,
            diag,
            settings,
            "selftest_disconnect",
            "glm-test",
        )
        first = next(relay)
        check(b"response.created" in first, "断开测试未收到首事件")
        relay.close()
        check(
            wait_for(
                lambda: any(
                    item["event"] == "Codex侧断开"
                    and item["request_id"] == "selftest_disconnect"
                    for item in diag.tail(2000)
                )
            ),
            "Codex 侧主动断开未记录",
        )

    record("Codex 客户端主动断开", client_disconnect)

    def log_redaction() -> None:
        log_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (log_dir / "proxy.log", log_dir / "events.jsonl")
        )
        for secret in (
            "SELF_TEST_AUTH_SECRET",
            "SELF_TEST_COOKIE_SECRET",
            "SELF_TEST_PROMPT_SECRET",
            "SELF_TEST_LOG_SECRET",
        ):
            check(secret not in log_text, f"日志泄漏测试值：{secret}")
        check("***REDACTED***" in log_text, "日志没有留下脱敏标记")

    record("Authorization/Cookie/正文日志脱敏", log_redaction)

    def waitress_options() -> None:
        adjustment = Adjustments(
            host="127.0.0.1",
            port=8765,
            threads=32,
            channel_timeout=660,
            cleanup_interval=15,
            clear_untrusted_proxy_headers=True,
        )
        check(adjustment.threads == 32, "Waitress threads 参数不兼容")
        check(adjustment.clear_untrusted_proxy_headers is True, "Waitress 安全参数不兼容")

    record("Waitress 启动参数兼容性", waitress_options)

    def configured_http_probe() -> None:
        result = proxy._configured_http_probe(settings, diag)
        check(result.get("ok") is True, f"启动 HTTP 探测失败：{result}")
        check(result.get("status") == 200, "启动 HTTP 探测状态异常")
        check(result.get("transport", {}).get("http_version") == "HTTP/1.1", "启动探测未使用 HTTP/1.1")

    record("启动无 API Key HTTP 探测", configured_http_probe)

    print("\nCodex 智谱诊断代理本地自检")
    print("=" * 48)
    for name, ok, detail in results:
        print(f"[{'通过' if ok else '失败'}] {name}: {detail}")
    print("=" * 48)
    failed = [item for item in results if not item[1]]
    print(f"合计：{len(results) - len(failed)} 通过，{len(failed)} 失败")

    local.close()
    local.task_dispatcher.shutdown()
    local_thread.join(timeout=2.0)
    upstream.shutdown()
    upstream.server_close()
    shared_client = app.extensions.get("bigmodel_shared_upstream_client")
    if shared_client is not None:
        shared_client.close()
    if keep_logs:
        destination = Path.cwd() / "logs" / f"self-test-{time.strftime('%Y%m%d-%H%M%S')}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(parents=True, exist_ok=True)
        for path in log_dir.iterdir():
            if path.is_file():
                (destination / path.name).write_bytes(path.read_bytes())
        print(f"自检日志已保留：{destination}")
    diag.close()
    temporary.cleanup()
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地模拟正常/静默/断流场景，不访问智谱")
    parser.add_argument("--keep-logs", action="store_true", help="把自检日志保留到 logs 目录")
    args = parser.parse_args()
    return run_self_test(keep_logs=args.keep_logs)


if __name__ == "__main__":
    raise SystemExit(main())
