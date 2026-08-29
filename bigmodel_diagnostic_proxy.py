#!/usr/bin/env python3
"""Codex -> 智谱 Responses API 的本地透明诊断代理。

设计目标：
1. 不把 Chat Completions 改写为 Responses；上游 SSE 事件内容透明转发。
2. 默认强制 HTTP/1.1、每次请求使用新上游连接，避开坏连接复用/HTTP2 中间盒问题。
3. 用中文日志记录 DNS、TLS、响应头、首字节、SSE 进度、静默和断流位置。
4. Authorization、Cookie 和请求正文默认不落盘。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import queue
import re
import socket
import ssl
import sys
import threading
import time
import traceback
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
import uuid
import zipfile

import httpx
from flask import Flask, Response, jsonify, request, send_file
from werkzeug.datastructures import Headers


APP_VERSION = "0.3.0"
BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """加载简单 KEY=VALUE 文件，不覆盖系统环境变量。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class Settings:
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    upstream_origin: str = "https://open.bigmodel.cn"
    log_dir: str = str(BASE_DIR / "logs")
    log_level: str = "INFO"
    log_body: bool = False
    log_body_max_chars: int = 6000
    log_max_mb: int = 20
    log_backup_count: int = 8
    transport_profile: str = "nvidia"
    force_http1: bool = True
    connection_close: bool = False
    reuse_upstream_client: bool = True
    follow_redirects: bool = True
    trust_env_proxy: bool = True
    insecure_skip_verify: bool = False
    ca_bundle: str = ""
    connect_timeout_seconds: float = 20.0
    read_timeout_seconds: float = 600.0
    write_timeout_seconds: float = 120.0
    pool_timeout_seconds: float = 20.0
    local_heartbeat_seconds: float = 10.0
    progress_every_events: int = 100
    progress_every_seconds: float = 30.0
    synthesize_failure_event: bool = True
    queue_chunks: int = 256
    startup_probe: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        bind_host = os.environ.get("BIGMODEL_PROXY_HOST", "127.0.0.1").strip()
        if bind_host.lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "BIGMODEL_PROXY_HOST 只允许 127.0.0.1、localhost 或 ::1；"
                "该代理会转发 API Key，禁止对局域网/公网监听"
            )
        upstream = os.environ.get(
            "BIGMODEL_UPSTREAM_ORIGIN", "https://open.bigmodel.cn"
        ).strip().rstrip("/")
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "BIGMODEL_UPSTREAM_ORIGIN 必须是 http/https URL，例如 "
                "https://open.bigmodel.cn"
            )
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"BIGMODEL_UPSTREAM_ORIGIN 端口无效：{exc}") from exc
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "BIGMODEL_UPSTREAM_ORIGIN 不允许包含用户名、密码、查询参数或片段"
            )
        if parsed.path not in {"", "/"}:
            raise ValueError(
                "BIGMODEL_UPSTREAM_ORIGIN 只填写站点根地址；请用 "
                "https://open.bigmodel.cn，不要附加 /api/v1"
            )
        transport_profile = os.environ.get(
            "BIGMODEL_TRANSPORT_PROFILE", "nvidia"
        ).strip().lower()
        if transport_profile not in {"nvidia", "isolated"}:
            raise ValueError(
                "BIGMODEL_TRANSPORT_PROFILE 只能是 nvidia 或 isolated"
            )
        nvidia_profile = transport_profile == "nvidia"
        return cls(
            bind_host=bind_host,
            bind_port=_env_int("BIGMODEL_PROXY_PORT", 8765, 1, 65535),
            upstream_origin=upstream,
            log_dir=os.environ.get("BIGMODEL_LOG_DIR", str(BASE_DIR / "logs")).strip(),
            log_level=os.environ.get("BIGMODEL_LOG_LEVEL", "INFO").strip().upper(),
            log_body=_env_bool("BIGMODEL_LOG_BODY", False),
            log_body_max_chars=_env_int("BIGMODEL_LOG_BODY_MAX_CHARS", 6000, 500, 100000),
            log_max_mb=_env_int("BIGMODEL_LOG_MAX_MB", 20, 1, 1024),
            log_backup_count=_env_int("BIGMODEL_LOG_BACKUPS", 8, 1, 100),
            transport_profile=transport_profile,
            force_http1=True
            if nvidia_profile
            else _env_bool("BIGMODEL_FORCE_HTTP1", True),
            connection_close=False
            if nvidia_profile
            else _env_bool("BIGMODEL_CONNECTION_CLOSE", True),
            reuse_upstream_client=True
            if nvidia_profile
            else _env_bool("BIGMODEL_REUSE_UPSTREAM_CLIENT", False),
            follow_redirects=True
            if nvidia_profile
            else _env_bool("BIGMODEL_FOLLOW_REDIRECTS", False),
            trust_env_proxy=_env_bool("BIGMODEL_TRUST_ENV_PROXY", True),
            insecure_skip_verify=_env_bool("BIGMODEL_INSECURE_SKIP_VERIFY", False),
            ca_bundle=os.environ.get("BIGMODEL_CA_BUNDLE", "").strip(),
            connect_timeout_seconds=_env_float("BIGMODEL_CONNECT_TIMEOUT", 20, 1, 300),
            read_timeout_seconds=_env_float("BIGMODEL_READ_TIMEOUT", 600, 0, 7200),
            write_timeout_seconds=_env_float("BIGMODEL_WRITE_TIMEOUT", 120, 1, 7200),
            pool_timeout_seconds=_env_float("BIGMODEL_POOL_TIMEOUT", 20, 1, 300),
            local_heartbeat_seconds=_env_float("BIGMODEL_LOCAL_HEARTBEAT", 10, 0, 300),
            progress_every_events=_env_int("BIGMODEL_PROGRESS_EVENTS", 100, 1, 100000),
            progress_every_seconds=_env_float("BIGMODEL_PROGRESS_SECONDS", 30, 1, 3600),
            synthesize_failure_event=_env_bool("BIGMODEL_SYNTHESIZE_FAILURE_EVENT", True),
            queue_chunks=_env_int("BIGMODEL_QUEUE_CHUNKS", 256, 8, 8192),
            startup_probe=_env_bool("BIGMODEL_STARTUP_PROBE", True),
        )

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data.get("ca_bundle"):
            data["ca_bundle"] = str(Path(data["ca_bundle"]).name)
        return data


SENSITIVE_NAME_RE = re.compile(
    r"authorization|api[-_]?key|token|secret|password|cookie|credential", re.I
)
CONTENT_NAME_RE = re.compile(r"content|input|instructions|prompt|text", re.I)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _redact_text(text: str) -> str:
    # URL 中的 user:password@host 和常见查询参数也必须脱敏，避免异常文本把
    # 显式代理凭据或临时 token 带进日志。
    text = re.sub(
        r"(?i)(https?://)([^/\s:@]+):([^@\s/]+)@",
        r"\1***:***@",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:api[-_]?key|access[-_]?token|token|key|authorization)=)[^&\s]+",
        r"\1***REDACTED***",
        text,
    )
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;\"']+",
        r"\1***REDACTED***",
        text,
    )
    text = re.sub(
        r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}",
        r"\1***REDACTED***",
        text,
    )
    text = re.sub(
        r"(?i)((?:api[-_]?key|token|secret|password)\s*[:=]\s*)[^\s,;\"']+",
        r"\1***REDACTED***",
        text,
    )
    return text


def _redact_value(value: Any, key: str = "") -> Any:
    """递归脱敏所有日志字段；调用者忘记脱敏时仍有最后一道保护。"""
    if SENSITIVE_NAME_RE.search(key):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(k): _redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(v, key) for v in value]
    if isinstance(value, str):
        return _redact_text(value)
    return _json_safe(value)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": _utc_iso(),
            "level": record.levelname,
            "event": getattr(record, "event", "log"),
            "request_id": getattr(record, "request_id", ""),
            "message": _redact_text(record.getMessage()),
            "fields": _json_safe(getattr(record, "fields", {})),
        }
        if record.exc_info:
            payload["exception"] = _redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", "") or "-"
        event = getattr(record, "event", "log")
        fields = getattr(record, "fields", {}) or {}
        suffix = ""
        if fields:
            suffix = " | " + json.dumps(_json_safe(fields), ensure_ascii=False, separators=(",", ":"))
        return (
            f"{_utc_iso()} {record.levelname:<7} [{request_id}] "
            f"{event} - {_redact_text(record.getMessage())}{suffix}"
        )


class DiagnosticLogger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log_dir = Path(settings.log_dir).expanduser().resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.recent: deque[dict[str, Any]] = deque(maxlen=2000)
        self.recent_lock = threading.Lock()
        self.logger = logging.getLogger(f"bigmodel-diagnostic-proxy-{id(self)}")
        self.logger.propagate = False
        self.logger.handlers.clear()
        level = getattr(logging, settings.log_level, logging.INFO)
        self.logger.setLevel(level)

        human_formatter = _HumanFormatter()
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(human_formatter)
        self.logger.addHandler(console)

        human_file = RotatingFileHandler(
            self.log_dir / "proxy.log",
            maxBytes=settings.log_max_mb * 1024 * 1024,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        human_file.setLevel(level)
        human_file.setFormatter(human_formatter)
        self.logger.addHandler(human_file)

        json_file = RotatingFileHandler(
            self.log_dir / "events.jsonl",
            maxBytes=settings.log_max_mb * 1024 * 1024,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        json_file.setLevel(level)
        json_file.setFormatter(_JsonFormatter())
        self.logger.addHandler(json_file)

    def emit(
        self,
        level: int,
        event: str,
        message: str,
        request_id: str = "",
        **fields: Any,
    ) -> None:
        safe_message = _redact_text(str(message))
        safe_fields = _redact_value(fields)
        record = {
            "time": _utc_iso(),
            "level": logging.getLevelName(level),
            "event": event,
            "request_id": request_id,
            "message": safe_message,
            "fields": safe_fields,
        }
        with self.recent_lock:
            self.recent.append(record)
        self.logger.log(
            level,
            safe_message,
            extra={"event": event, "request_id": request_id, "fields": safe_fields},
        )

    def tail(self, limit: int) -> list[dict[str, Any]]:
        with self.recent_lock:
            items = list(self.recent)
        return items[-max(1, min(limit, 2000)) :]

    def close(self) -> None:
        """刷新并关闭日志文件；主要供自检和短命令安全释放 Windows 文件句柄。"""
        handlers = list(self.logger.handlers)
        self.logger.handlers.clear()
        for handler in handlers:
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass


def _flatten_cert_name(parts: Any) -> str:
    values: list[str] = []
    try:
        for group in parts or []:
            for key, value in group:
                values.append(f"{key}={value}")
    except Exception:
        return str(parts or "")
    return ", ".join(values)


def _ssl_context(settings: Settings) -> ssl.SSLContext:
    if settings.insecure_skip_verify:
        return ssl._create_unverified_context()  # noqa: SLF001 - 显式诊断开关
    if settings.ca_bundle:
        return ssl.create_default_context(cafile=settings.ca_bundle)
    # Windows Python 会加载 Windows ROOT/CA 证书库；这对企业 TLS 检查尤为重要。
    return ssl.create_default_context()


def _socket_options() -> list[tuple[int, int, int]]:
    options: list[tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 5)):
        option = getattr(socket, name, None)
        if option is not None:
            options.append((socket.IPPROTO_TCP, option, value))
    return options


def _http_client(settings: Settings) -> httpx.Client:
    read_timeout: float | None = settings.read_timeout_seconds or None
    timeout = httpx.Timeout(
        connect=settings.connect_timeout_seconds,
        read=read_timeout,
        write=settings.write_timeout_seconds,
        pool=settings.pool_timeout_seconds,
    )
    if settings.transport_profile == "nvidia":
        # codex-nvidia-proxy 使用 OpenAI SDK 的默认同步客户端。其核心行为是：
        # HTTP/1.1、共享线程安全连接池、允许 keep-alive、继承代理环境。
        # 此处仍做 Responses 字节透明转发，只复刻连接层，不引入格式转换。
        return httpx.Client(
            verify=_ssl_context(settings),
            timeout=timeout,
            trust_env=settings.trust_env_proxy,
            http1=True,
            http2=False,
            follow_redirects=settings.follow_redirects,
            limits=httpx.Limits(
                max_connections=1000,
                max_keepalive_connections=100,
                keepalive_expiry=5.0,
            ),
        )

    limits = httpx.Limits(
        max_connections=64,
        max_keepalive_connections=0 if settings.connection_close else 16,
        keepalive_expiry=5.0 if settings.connection_close else 60.0,
    )
    transport = httpx.HTTPTransport(
        verify=_ssl_context(settings),
        trust_env=settings.trust_env_proxy,
        http1=True,
        http2=not settings.force_http1,
        limits=limits,
        retries=0,
        socket_options=_socket_options(),
    )
    return httpx.Client(
        transport=transport,
        timeout=timeout,
        trust_env=settings.trust_env_proxy,
        follow_redirects=settings.follow_redirects,
    )


def _request_summary(body: bytes, content_type: str) -> tuple[dict[str, Any], Any | None]:
    summary: dict[str, Any] = {
        "body_bytes": len(body),
        "body_sha256_16": hashlib.sha256(body).hexdigest()[:16],
        "content_type": content_type,
    }
    if not body or "json" not in content_type.lower():
        return summary, None
    try:
        data = json.loads(body)
    except Exception as exc:
        summary["json_parse_error"] = f"{type(exc).__name__}: {exc}"
        return summary, None
    if not isinstance(data, dict):
        summary["json_root_type"] = type(data).__name__
        return summary, data
    summary.update(
        {
            "model": data.get("model"),
            "stream": data.get("stream"),
            "has_previous_response_id": bool(data.get("previous_response_id")),
            "tool_count": len(data.get("tools") or []) if isinstance(data.get("tools"), list) else None,
            "input_type": type(data.get("input")).__name__ if "input" in data else None,
            "input_items": len(data.get("input") or []) if isinstance(data.get("input"), list) else None,
        }
    )
    return summary, data


def _sanitized_body(data: Any, max_chars: int) -> str:
    def clean(value: Any, key: str = "") -> Any:
        if SENSITIVE_NAME_RE.search(key):
            return "***REDACTED***"
        if isinstance(value, dict):
            return {str(k): clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v, key) for v in value]
        if CONTENT_NAME_RE.search(key) and isinstance(value, str) and len(value) > 1500:
            return value[:1500] + "…[正文已截断]"
        return value

    text = json.dumps(clean(data), ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "…[请求体日志已截断]"
    return _redact_text(text)


def _error_body_summary(body: bytes, status: int | None = None) -> dict[str, Any]:
    """提取足够定位 401/429/WAF 的错误信息，但不把完整响应正文写入日志。"""
    result: dict[str, Any] = {
        "body_bytes": len(body),
        "body_sha256_16": hashlib.sha256(body).hexdigest()[:16],
    }
    text = body[:32768].decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except Exception:
        # 非 JSON 多为网关/WAF HTML；只保留短预览。
        result["format"] = "text_or_html"
        result["preview"] = _redact_text(text[:1200])
        result["truncated"] = len(text) > 1200 or len(body) > 32768
        lowered = text.lower()
        if "content filter" in lowered and "access denied" in lowered:
            result["classification"] = "enterprise_proxy_content_filter_denied"
            result["diagnosis_cn"] = (
                "疑似企业显式代理/内容过滤器拒绝；不是智谱 Responses JSON 错误"
            )
        elif status == 504 and any(
            marker in lowered for marker in ("proxy", "gateway timeout", "access denied")
        ):
            result["classification"] = "gateway_or_proxy_timeout"
            result["diagnosis_cn"] = "疑似中间网关或显式代理超时"
        return result

    result["format"] = "json"
    if not isinstance(data, dict):
        result["root_type"] = type(data).__name__
        return result
    result["top_level_keys"] = sorted(str(k) for k in data.keys())[:50]
    error = data.get("error")
    if isinstance(error, dict):
        for name in ("type", "code", "param", "status"):
            if name in error:
                result[f"error_{name}"] = _redact_value(error.get(name), name)
        if "message" in error:
            result["error_message"] = _redact_text(str(error.get("message")))[:2000]
    elif error is not None:
        result["error"] = _redact_text(str(error))[:2000]
    elif "message" in data:
        result["message"] = _redact_text(str(data.get("message")))[:2000]
    return result


def _forward_headers(source: Iterable[tuple[str, str]], settings: Settings) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in source:
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        headers[name] = value
    # 便于按原始 SSE 字节诊断，避免压缩层遮蔽事件边界。
    headers["Accept-Encoding"] = "identity"
    if settings.connection_close:
        headers["Connection"] = "close"
    return headers


def _response_headers(source: httpx.Headers, request_id: str) -> Headers:
    result = Headers()
    for name, value in source.multi_items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"content-encoding"}:
            continue
        result.add(name, value)
    result["X-Local-Proxy-Request-ID"] = request_id
    result["X-Accel-Buffering"] = "no"
    result["Cache-Control"] = result.get("Cache-Control", "no-cache")
    return result


def _trace_headers(headers: httpx.Headers) -> dict[str, str]:
    wanted = {
        "x-log-id",
        "ga-traceid",
        "x-request-id",
        "request-id",
        "traceparent",
        "server",
        "via",
        "content-type",
        "transfer-encoding",
        "connection",
    }
    return {k: v for k, v in headers.items() if k.lower() in wanted}


def _transport_details(response: httpx.Response) -> dict[str, Any]:
    details: dict[str, Any] = {
        "http_version": response.http_version,
    }
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return details
    try:
        ssl_object = stream.get_extra_info("ssl_object")
        if ssl_object is not None:
            cert = ssl_object.getpeercert() or {}
            cert_der = ssl_object.getpeercert(binary_form=True)
            details.update(
                {
                    "tls_version": ssl_object.version(),
                    "tls_cipher": (ssl_object.cipher() or [None])[0],
                    "cert_subject": _flatten_cert_name(cert.get("subject")),
                    "cert_issuer": _flatten_cert_name(cert.get("issuer")),
                    "cert_sha256_16": hashlib.sha256(cert_der).hexdigest()[:16]
                    if cert_der
                    else None,
                }
            )
    except Exception as exc:
        details["tls_detail_error"] = f"{type(exc).__name__}: {exc}"
    try:
        sock = stream.get_extra_info("socket")
        if sock is not None:
            details["peer"] = str(sock.getpeername())
            details["local"] = str(sock.getsockname())
    except Exception as exc:
        details["socket_detail_error"] = f"{type(exc).__name__}: {exc}"
    return details


class SseTracker:
    def __init__(
        self,
        diag: DiagnosticLogger,
        settings: Settings,
        request_id: str,
        model: str | None,
    ):
        self.diag = diag
        self.settings = settings
        self.request_id = request_id
        self.model = model
        self.response_id = ""
        self.buffer = b""
        self.bytes_total = 0
        self.chunk_count = 0
        self.event_count = 0
        self.event_types: Counter[str] = Counter()
        self.last_event_type = ""
        self.terminal = False
        self.first_byte_at: float | None = None
        self.first_event_at: float | None = None
        self.last_byte_at: float | None = None
        self.started_at = time.monotonic()
        self.last_progress_at = self.started_at
        self.last_progress_event_count = 0
        self.lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        now = time.monotonic()
        with self.lock:
            if self.first_byte_at is None:
                self.first_byte_at = now
                self.diag.emit(
                    logging.INFO,
                    "收到首块数据",
                    "已收到智谱返回的第一块响应数据",
                    self.request_id,
                    elapsed_ms=round((now - self.started_at) * 1000),
                    chunk_bytes=len(chunk),
                )
            self.last_byte_at = now
            self.bytes_total += len(chunk)
            self.chunk_count += 1
            self.buffer += chunk
            self._parse_complete_events_locked(now)
            if (
                self.event_count - self.last_progress_event_count
                >= self.settings.progress_every_events
                or now - self.last_progress_at >= self.settings.progress_every_seconds
            ):
                self._log_progress_locked(now)

    def finish(self) -> None:
        with self.lock:
            if self.buffer.strip():
                self._parse_event_locked(self.buffer, time.monotonic(), trailing=True)
            self.buffer = b""

    def _next_delimiter(self) -> tuple[int, int] | None:
        candidates: list[tuple[int, int]] = []
        for delimiter in (b"\n\n", b"\r\n\r\n"):
            index = self.buffer.find(delimiter)
            if index >= 0:
                candidates.append((index, len(delimiter)))
        return min(candidates, default=None)

    def _parse_complete_events_locked(self, now: float) -> None:
        while True:
            found = self._next_delimiter()
            if found is None:
                # 防止畸形上游持续不给事件分隔符而无限占内存。
                if len(self.buffer) > 4 * 1024 * 1024:
                    self.diag.emit(
                        logging.WARNING,
                        "SSE事件过大",
                        "单个未分隔 SSE 事件已超过 4 MiB，仅保留尾部用于诊断",
                        self.request_id,
                        buffered_bytes=len(self.buffer),
                    )
                    self.buffer = self.buffer[-1024 * 1024 :]
                return
            index, delimiter_size = found
            block = self.buffer[:index]
            self.buffer = self.buffer[index + delimiter_size :]
            self._parse_event_locked(block, now)

    def _parse_event_locked(self, block: bytes, now: float, trailing: bool = False) -> None:
        if not block.strip():
            return
        event_name = ""
        data_lines: list[bytes] = []
        comment_only = True
        for raw_line in block.splitlines():
            line = raw_line.strip(b"\r")
            if line.startswith(b"event:"):
                event_name = line[6:].strip().decode("utf-8", errors="replace")
                comment_only = False
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
                comment_only = False
            elif line and not line.startswith(b":"):
                comment_only = False
        if comment_only:
            self.event_types["上游注释心跳"] += 1
            return

        data = b"\n".join(data_lines)
        payload_type = ""
        if data == b"[DONE]":
            payload_type = "[DONE]"
        elif data:
            try:
                obj = json.loads(data)
                if isinstance(obj, dict):
                    payload_type = str(obj.get("type") or "")
                    response_obj = obj.get("response")
                    if isinstance(response_obj, dict):
                        self.response_id = str(response_obj.get("id") or self.response_id)
                        self.model = str(response_obj.get("model") or self.model or "")
                    self.response_id = str(obj.get("id") or self.response_id)
                    self.model = str(obj.get("model") or self.model or "")
            except Exception:
                payload_type = "data非JSON"
        event_type = event_name or payload_type or "未命名事件"
        self.event_count += 1
        self.event_types[event_type] += 1
        self.last_event_type = event_type
        if self.first_event_at is None:
            self.first_event_at = now
            self.diag.emit(
                logging.INFO,
                "收到首个SSE事件",
                f"收到首个 SSE 事件：{event_type}",
                self.request_id,
                elapsed_ms=round((now - self.started_at) * 1000),
                trailing=trailing,
            )
        if event_type in {"response.completed", "response.failed", "error", "[DONE]"}:
            self.terminal = True
            self.diag.emit(
                logging.INFO if event_type in {"response.completed", "[DONE]"} else logging.WARNING,
                "收到终止事件",
                f"收到流终止事件：{event_type}",
                self.request_id,
                event_count=self.event_count,
                bytes_total=self.bytes_total,
            )
        elif event_type in {
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.output_item.done",
        }:
            self.diag.emit(
                logging.DEBUG,
                "SSE生命周期事件",
                f"收到事件：{event_type}",
                self.request_id,
                event_index=self.event_count,
            )

    def _log_progress_locked(self, now: float) -> None:
        silence_ms = (
            round((now - self.last_byte_at) * 1000) if self.last_byte_at is not None else None
        )
        self.diag.emit(
            logging.INFO,
            "SSE流进度",
            "智谱 SSE 流仍在传输",
            self.request_id,
            elapsed_ms=round((now - self.started_at) * 1000),
            bytes_total=self.bytes_total,
            chunks=self.chunk_count,
            events=self.event_count,
            last_event=self.last_event_type,
            silence_ms=silence_ms,
        )
        self.last_progress_at = now
        self.last_progress_event_count = self.event_count

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            return {
                "elapsed_ms": round((now - self.started_at) * 1000),
                "bytes_total": self.bytes_total,
                "chunks": self.chunk_count,
                "events": self.event_count,
                "event_types": dict(self.event_types),
                "last_event": self.last_event_type,
                "terminal": self.terminal,
                "response_id": self.response_id,
                "model": self.model,
                # 只有位于 SSE 事件边界时才能插入注释心跳。若上游刚好在一个
                # data: 行中间停住，强行插入会破坏 JSON 和会话流。
                "heartbeat_safe": not self.buffer,
                "buffered_event_bytes": len(self.buffer),
                "first_byte_ms": round((self.first_byte_at - self.started_at) * 1000)
                if self.first_byte_at is not None
                else None,
                "first_event_ms": round((self.first_event_at - self.started_at) * 1000)
                if self.first_event_at is not None
                else None,
                "silence_ms": round((now - self.last_byte_at) * 1000)
                if self.last_byte_at is not None
                else None,
            }


def _failure_sse(request_id: str, tracker: SseTracker, reason: str) -> bytes:
    snapshot = tracker.snapshot()
    response_id = snapshot.get("response_id") or f"resp_proxy_{uuid.uuid4().hex[:12]}"
    payload = {
        "type": "response.failed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "failed",
            "model": snapshot.get("model") or "unknown",
            "error": {
                "code": "local_proxy_upstream_disconnected",
                "type": "upstream_connection_error",
                "message": f"智谱上游流异常结束；本地诊断请求号 {request_id}；{reason}",
            },
            "output": [],
            "usage": None,
        },
    }
    return (
        "event: response.failed\n"
        + "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")


def _queue_put(
    output: queue.Queue[tuple[str, Any]],
    item: tuple[str, Any],
    cancel: threading.Event,
) -> bool:
    while not cancel.is_set():
        try:
            output.put(item, timeout=0.5)
            return True
        except queue.Full:
            continue
    return False


def _close_client_if_owned(client: httpx.Client, close_client: bool) -> None:
    if not close_client:
        return
    try:
        client.close()
    except Exception:
        pass


def _sse_relay(
    response: httpx.Response,
    client: httpx.Client,
    close_client: bool,
    diag: DiagnosticLogger,
    settings: Settings,
    request_id: str,
    model: str | None,
) -> Iterable[bytes]:
    output: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=settings.queue_chunks)
    cancel = threading.Event()
    tracker = SseTracker(diag, settings, request_id, model)

    def read_upstream() -> None:
        try:
            # Accept-Encoding 已强制为 identity；这里仍使用 iter_bytes()，即使某个
            # 网关无视 identity 返回压缩内容，也会先解压再交给 SSE 解析器。
            for chunk in response.iter_bytes():
                if cancel.is_set():
                    break
                if not chunk:
                    continue
                tracker.feed(chunk)
                if not _queue_put(output, ("data", chunk), cancel):
                    return
            tracker.finish()
            _queue_put(output, ("eof", None), cancel)
        except Exception as exc:
            _queue_put(output, ("error", exc), cancel)
        finally:
            try:
                response.close()
            except Exception:
                pass
            _close_client_if_owned(client, close_client)

    producer = threading.Thread(
        target=read_upstream,
        name=f"bigmodel-upstream-{request_id}",
        daemon=True,
    )
    producer.start()
    heartbeat_timeouts = 0
    local_heartbeats = 0
    try:
        while True:
            try:
                if settings.local_heartbeat_seconds > 0:
                    kind, value = output.get(timeout=settings.local_heartbeat_seconds)
                else:
                    kind, value = output.get()
            except queue.Empty:
                heartbeat_timeouts += 1
                snapshot = tracker.snapshot()
                if snapshot.get("heartbeat_safe"):
                    local_heartbeats += 1
                    diag.emit(
                        logging.WARNING,
                        "上游静默",
                        "等待智谱数据期间向 Codex 发送本地 SSE 注释心跳",
                        request_id,
                        local_heartbeat_index=local_heartbeats,
                        silence_check_index=heartbeat_timeouts,
                        upstream_silence_ms=snapshot.get("silence_ms"),
                        elapsed_ms=snapshot.get("elapsed_ms"),
                    )
                    yield f": local-proxy-heartbeat request={request_id} index={local_heartbeats}\n\n".encode(
                        "utf-8"
                    )
                else:
                    diag.emit(
                        logging.WARNING,
                        "上游静默且事件未闭合",
                        "智谱停在一个未闭合 SSE 事件中；为避免破坏 JSON，本次不插入心跳",
                        request_id,
                        silence_check_index=heartbeat_timeouts,
                        buffered_event_bytes=snapshot.get("buffered_event_bytes"),
                        upstream_silence_ms=snapshot.get("silence_ms"),
                        elapsed_ms=snapshot.get("elapsed_ms"),
                    )
                continue

            if kind == "data":
                yield value
                continue
            if kind == "error":
                snapshot = tracker.snapshot()
                error_text = f"{type(value).__name__}: {value}"
                diag.emit(
                    logging.ERROR,
                    "上游读取异常",
                    "读取智谱 SSE 流时发生异常",
                    request_id,
                    error=error_text,
                    **snapshot,
                )
                if settings.synthesize_failure_event and not snapshot.get("terminal"):
                    yield _failure_sse(request_id, tracker, error_text)
                break
            if kind == "eof":
                snapshot = tracker.snapshot()
                if snapshot.get("terminal"):
                    diag.emit(
                        logging.INFO,
                        "请求正常结束",
                        "智谱上游在终止事件后正常关闭响应流",
                        request_id,
                        local_heartbeats=local_heartbeats,
                        **snapshot,
                    )
                else:
                    diag.emit(
                        logging.ERROR,
                        "上游提前断流",
                        "智谱上游已到 EOF，但未看到 response.completed/response.failed/[DONE]",
                        request_id,
                        local_heartbeats=local_heartbeats,
                        **snapshot,
                    )
                    if settings.synthesize_failure_event:
                        yield _failure_sse(request_id, tracker, "上游提前 EOF，缺少终止事件")
                break
    except GeneratorExit:
        diag.emit(
            logging.WARNING,
            "Codex侧断开",
            "Codex/本地客户端先关闭了与代理的响应流",
            request_id,
            **tracker.snapshot(),
        )
        raise
    except Exception as exc:
        diag.emit(
            logging.ERROR,
            "本地转发异常",
            "代理向 Codex 输出 SSE 时发生异常",
            request_id,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8),
            **tracker.snapshot(),
        )
        raise
    finally:
        cancel.set()
        try:
            response.close()
        except Exception:
            pass
        _close_client_if_owned(client, close_client)
        producer.join(timeout=1.5)


def _generic_relay(
    response: httpx.Response,
    client: httpx.Client,
    close_client: bool,
    diag: DiagnosticLogger,
    request_id: str,
) -> Iterable[bytes]:
    total = 0
    started = time.monotonic()
    try:
        for chunk in response.iter_bytes():
            if chunk:
                total += len(chunk)
                yield chunk
        diag.emit(
            logging.INFO,
            "普通响应结束",
            "非 SSE 上游响应已转发完成",
            request_id,
            bytes_total=total,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
    except GeneratorExit:
        diag.emit(
            logging.WARNING,
            "Codex侧断开",
            "Codex/本地客户端先关闭了普通响应",
            request_id,
            bytes_total=total,
        )
        raise
    except Exception as exc:
        diag.emit(
            logging.ERROR,
            "普通响应读取异常",
            "读取非 SSE 上游响应时发生异常",
            request_id,
            bytes_total=total,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        try:
            response.close()
        except Exception:
            pass
        _close_client_if_owned(client, close_client)


def _target_url(settings: Settings, incoming_path: str, query: bytes) -> str:
    if incoming_path.startswith("/api/v1"):
        path = incoming_path
    elif incoming_path.startswith("/v1"):
        path = "/api" + incoming_path
    else:
        path = "/api/v1" + (incoming_path if incoming_path.startswith("/") else "/" + incoming_path)
    parsed = urlsplit(settings.upstream_origin)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query.decode("latin-1"), ""))


def _dns_snapshot(hostname: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        seen: set[tuple[Any, ...]] = set()
        for family, socktype, proto, canonname, sockaddr in answers:
            key = (family, sockaddr)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "family": "IPv6" if family == socket.AF_INET6 else "IPv4",
                    "address": sockaddr[0],
                    "port": sockaddr[1],
                    "canonname": canonname,
                }
            )
    except Exception as exc:
        rows.append({"error": f"{type(exc).__name__}: {exc}"})
    return rows


def _proxy_env_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        value = os.environ.get(name) or os.environ.get(name.lower())
        if not value:
            result[name] = {"present": False}
            continue
        if name == "NO_PROXY":
            entries = [item.strip().lower() for item in value.split(",") if item.strip()]
            loopback_names = {"127.0.0.1", "localhost", "::1", "[::1]", "*"}
            result[name] = {
                "present": True,
                "entry_count": len(entries),
                "loopback_covered": any(item in loopback_names for item in entries),
            }
            continue
        try:
            parsed = urlsplit(value if "://" in value else "http://" + value)
            result[name] = {
                "present": True,
                "scheme": parsed.scheme,
                "host": parsed.hostname,
                "port": parsed.port,
                "userinfo_present": bool(parsed.username or parsed.password),
            }
        except Exception as exc:
            result[name] = {
                "present": True,
                "parse_ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return result


def _configured_http_probe(settings: Settings, diag: DiagnosticLogger) -> dict[str, Any]:
    """不带 API Key，经当前 httpx/代理配置探测模型端点。"""
    target = settings.upstream_origin + "/api/v1/models"
    started = time.monotonic()
    client = _http_client(settings)
    try:
        response = client.get(
            target,
            headers={
                "Accept": "application/json",
                "User-Agent": f"codex-bigmodel-diagnostic-proxy/{APP_VERSION}",
            },
            timeout=30.0,
        )
        body = response.content
        summary = _error_body_summary(body, response.status_code)
        result = {
            "ok": response.status_code < 500,
            "status": response.status_code,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "trace_headers": _trace_headers(response.headers),
            "transport": _transport_details(response),
            "body_summary": summary,
        }
        proxy_denied = (
            summary.get("classification")
            == "enterprise_proxy_content_filter_denied"
        )
        diag.emit(
            logging.ERROR if response.status_code >= 500 or proxy_denied else logging.INFO,
            "启动HTTP探测被内容过滤拒绝" if proxy_denied else "启动HTTP探测完成",
            (
                "无 API Key 的 GET 探测收到 Content Filter - Access Denied"
                if proxy_denied
                else f"无 API Key 的 GET 探测返回 HTTP {response.status_code}"
            ),
            status=response.status_code,
            elapsed_ms=result["elapsed_ms"],
            trace_headers=result["trace_headers"],
            transport=result["transport"],
            body_summary=summary,
        )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }
        diag.emit(
            logging.ERROR,
            "启动HTTP探测失败",
            "无 API Key 的 GET 探测在收到 HTTP 响应前失败",
            **result,
        )
        return result
    finally:
        client.close()


def run_startup_probe(settings: Settings, diag: DiagnosticLogger) -> dict[str, Any]:
    parsed = urlsplit(settings.upstream_origin)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    result: dict[str, Any] = {
        "time": _utc_iso(),
        "hostname": hostname,
        "port": port,
        "dns": _dns_snapshot(hostname),
        "proxy_env": _proxy_env_snapshot(),
    }
    diag.emit(
        logging.INFO,
        "启动网络快照",
        "已记录目标域名 DNS 与显式代理环境",
        dns=result["dns"],
        proxy_env=result["proxy_env"],
    )
    proxy_is_present = any(
        result["proxy_env"].get(name, {}).get("present")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    )
    no_proxy_state = result["proxy_env"].get("NO_PROXY", {})
    if proxy_is_present and not no_proxy_state.get("loopback_covered"):
        diag.emit(
            logging.WARNING,
            "NO_PROXY缺少回环地址",
            "检测到显式代理，但 NO_PROXY 未覆盖回环地址；同一环境启动的 Codex 可能把本地请求误发给企业代理",
            proxy_env=result["proxy_env"],
        )
    result["http_via_configured_transport"] = _configured_http_probe(settings, diag)
    if parsed.scheme != "https":
        result["tls"] = {"skipped": "上游不是 HTTPS"}
        return result
    started = time.monotonic()
    try:
        raw_sock = socket.create_connection(
            (hostname, port), timeout=settings.connect_timeout_seconds
        )
        with raw_sock:
            with _ssl_context(settings).wrap_socket(raw_sock, server_hostname=hostname) as tls_sock:
                cert = tls_sock.getpeercert() or {}
                cert_der = tls_sock.getpeercert(binary_form=True)
                result["tls"] = {
                    "ok": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "version": tls_sock.version(),
                    "cipher": (tls_sock.cipher() or [None])[0],
                    "peer": str(tls_sock.getpeername()),
                    "subject": _flatten_cert_name(cert.get("subject")),
                    "issuer": _flatten_cert_name(cert.get("issuer")),
                    "sha256_16": hashlib.sha256(cert_der).hexdigest()[:16]
                    if cert_der
                    else None,
                }
        diag.emit(
            logging.INFO,
            "TLS探测成功",
            "已完成到智谱域名的独立 TLS 探测",
            **result["tls"],
        )
    except Exception as exc:
        result["tls"] = {
            "ok": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }
        diag.emit(
            logging.ERROR,
            "TLS探测失败",
            "独立 TLS 探测失败；若配置了显式 HTTPS_PROXY，此探测仍是直连探测",
            **result["tls"],
        )
    return result


INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex 智谱诊断代理</title>
<style>
body{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#111827;color:#e5e7eb;margin:0;padding:24px}
.card{background:#1f2937;border:1px solid #374151;border-radius:12px;padding:18px;margin-bottom:16px}
h1{font-size:24px;margin-top:0}.ok{color:#34d399}.warn{color:#fbbf24}code{background:#111827;padding:2px 6px;border-radius:4px}
button,a.btn{background:#2563eb;color:white;border:0;border-radius:7px;padding:9px 13px;text-decoration:none;cursor:pointer}
pre{white-space:pre-wrap;word-break:break-all;max-height:58vh;overflow:auto;background:#030712;padding:12px;border-radius:8px;font-size:12px}
</style></head><body>
<h1>Codex 智谱 Responses/SSE 诊断代理</h1>
<div class="card"><b class="ok">代理已运行</b><p>Codex Base URL：<code id="base"></code></p>
<p>日志默认不记录 Authorization 与对话正文。请求失败后请下载诊断包。</p>
<a class="btn" href="/api/diagnostics/export">下载诊断包</a> <button onclick="loadLogs()">刷新日志</button></div>
<div class="card"><b>最近中文日志</b><pre id="logs">加载中…</pre></div>
<script>
async function loadStatus(){let r=await fetch('/api/status');let d=await r.json();document.getElementById('base').textContent='http://'+location.hostname+':'+d.settings.bind_port+'/api/v1'}
async function loadLogs(){let r=await fetch('/api/logs?limit=300');let d=await r.json();document.getElementById('logs').textContent=d.items.map(x=>`${x.time} ${x.level.padEnd(7)} [${x.request_id||'-'}] ${x.event} - ${x.message} ${Object.keys(x.fields||{}).length?JSON.stringify(x.fields):''}`).join('\n')}
loadStatus();loadLogs();setInterval(loadLogs,5000);
</script></body></html>"""


def _codex_config_extract() -> str:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config = codex_home / "config.toml"
    if not config.exists():
        return f"未找到 {config}\n"
    safe_patterns = re.compile(
        r"^\s*(model|model_provider|model_catalog_json|wire_api|base_url|"
        r"request_max_retries|stream_max_retries|stream_idle_timeout_ms|"
        r"requires_openai_auth|name)\s*=|^\s*\[model_providers\.",
        re.I,
    )
    lines = []
    for line in config.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if safe_patterns.search(line):
            lines.append(line)
    return _redact_text("\n".join(lines) + "\n")


def create_diagnostic_zip(settings: Settings, probe: dict[str, Any] | None = None) -> Path:
    log_dir = Path(settings.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = log_dir / f"bigmodel-diagnostics-{stamp}.zip"
    summary = {
        "app_version": APP_VERSION,
        "created_at": _utc_iso(),
        "python": sys.version,
        "platform": platform.platform(),
        "settings": settings.public_dict(),
        "proxy_env": _proxy_env_snapshot(),
        "probe": probe,
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "summary.json", json.dumps(summary, ensure_ascii=False, indent=2)
        )
        zf.writestr("codex-provider-config.txt", _codex_config_extract())
        for path in sorted(log_dir.glob("proxy.log*")):
            if path.is_file():
                zf.write(path, f"logs/{path.name}")
        for path in sorted(log_dir.glob("events.jsonl*")):
            if path.is_file():
                zf.write(path, f"logs/{path.name}")
    return output


def create_app(settings: Settings, diag: DiagnosticLogger) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    probe_holder: dict[str, Any] = {"value": None}
    shared_upstream_client = (
        _http_client(settings) if settings.reuse_upstream_client else None
    )
    app.extensions["bigmodel_shared_upstream_client"] = shared_upstream_client

    def acquire_upstream_client() -> tuple[httpx.Client, bool]:
        if shared_upstream_client is not None:
            return shared_upstream_client, False
        return _http_client(settings), True

    @app.get("/")
    def index() -> Response:
        return Response(INDEX_HTML, content_type="text/html; charset=utf-8")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "version": APP_VERSION})

    @app.get("/api/status")
    def status():
        return jsonify(
            {
                "ok": True,
                "version": APP_VERSION,
                "settings": settings.public_dict(),
                "probe": probe_holder["value"],
            }
        )

    @app.get("/api/logs")
    def recent_logs():
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        return jsonify({"items": diag.tail(limit)})

    @app.get("/api/diagnostics/export")
    def export_diagnostics():
        output = create_diagnostic_zip(settings, probe_holder["value"])
        diag.emit(
            logging.INFO,
            "生成诊断包",
            "已生成脱敏诊断包",
            file=output.name,
        )
        return send_file(
            output,
            as_attachment=True,
            download_name=output.name,
            mimetype="application/zip",
        )

    def proxy_request(**_route_values: Any) -> Response:
        request_id = "bm_" + datetime.now().strftime("%H%M%S") + "_" + uuid.uuid4().hex[:8]
        started = time.monotonic()
        body = request.get_data(cache=False)
        content_type = request.headers.get("Content-Type", "")
        summary, parsed_body = _request_summary(body, content_type)
        target = _target_url(settings, request.path, request.query_string)
        parsed_target = urlsplit(target)
        diag.emit(
            logging.INFO,
            "请求开始",
            f"Codex 请求将转发至 {parsed_target.scheme}://{parsed_target.netloc}{parsed_target.path}",
            request_id,
            method=request.method,
            client=request.remote_addr,
            **summary,
        )
        if settings.log_body and parsed_body is not None:
            diag.emit(
                logging.WARNING,
                "请求体调试日志",
                "已按显式配置记录脱敏且截断的请求体",
                request_id,
                body=_sanitized_body(parsed_body, settings.log_body_max_chars),
            )
        headers = _forward_headers(request.headers.items(), settings)
        client, close_client = acquire_upstream_client()
        try:
            upstream_request = client.build_request(
                request.method,
                target,
                headers=headers,
                content=body,
            )
            diag.emit(
                logging.INFO,
                "开始连接上游",
                "开始建立到智谱的 HTTP 连接并等待响应头",
                request_id,
                transport_profile=settings.transport_profile,
                client_scope="shared" if not close_client else "per_request",
                force_http1=settings.force_http1,
                connection_close=settings.connection_close,
                follow_redirects=settings.follow_redirects,
                trust_env_proxy=settings.trust_env_proxy,
            )
            upstream = client.send(upstream_request, stream=True)
        except Exception as exc:
            _close_client_if_owned(client, close_client)
            elapsed_ms = round((time.monotonic() - started) * 1000)
            diag.emit(
                logging.ERROR,
                "上游连接失败",
                "尚未收到智谱响应头，连接/发送请求即失败",
                request_id,
                elapsed_ms=elapsed_ms,
                error=f"{type(exc).__name__}: {exc}",
                dns=_dns_snapshot(parsed_target.hostname or ""),
            )
            return jsonify(
                {
                    "error": {
                        "type": "local_proxy_connection_error",
                        "message": "本地代理连接智谱失败",
                        "request_id": request_id,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                }
            ), 502, {"X-Local-Proxy-Request-ID": request_id}

        header_ms = round((time.monotonic() - started) * 1000)
        transport = _transport_details(upstream)
        traces = _trace_headers(upstream.headers)
        diag.emit(
            logging.INFO if upstream.status_code < 400 else logging.WARNING,
            "收到上游响应头",
            f"智谱返回 HTTP {upstream.status_code}",
            request_id,
            elapsed_ms=header_ms,
            status=upstream.status_code,
            trace_headers=traces,
            transport=transport,
        )
        response_headers = _response_headers(upstream.headers, request_id)
        response_content_type = upstream.headers.get("content-type", "")
        is_sse = "text/event-stream" in response_content_type.lower()

        # 错误响应通常很小，先完整读取以把脱敏错误正文写入日志，便于区分限额/WAF/鉴权。
        if upstream.status_code >= 400:
            try:
                error_body = upstream.read()
            except Exception as exc:
                error_body = f"读取错误响应失败：{type(exc).__name__}: {exc}".encode("utf-8")
            finally:
                upstream.close()
                _close_client_if_owned(client, close_client)
            error_summary = _error_body_summary(error_body, upstream.status_code)
            proxy_denied = (
                error_summary.get("classification")
                == "enterprise_proxy_content_filter_denied"
            )
            diag.emit(
                logging.ERROR,
                "疑似企业代理内容过滤拒绝" if proxy_denied else "上游HTTP错误",
                (
                    "收到 Content Filter - Access Denied 页面；错误由企业代理/过滤器生成"
                    if proxy_denied
                    else f"智谱返回 HTTP {upstream.status_code}，已记录脱敏后的错误摘要"
                ),
                request_id,
                status=upstream.status_code,
                error_summary=error_summary,
                total_elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return Response(error_body, status=upstream.status_code, headers=response_headers)

        if is_sse:
            model = summary.get("model") if isinstance(summary.get("model"), str) else None
            return Response(
                _sse_relay(
                    upstream,
                    client,
                    close_client,
                    diag,
                    settings,
                    request_id,
                    model,
                ),
                status=upstream.status_code,
                headers=response_headers,
                direct_passthrough=True,
            )
        return Response(
            _generic_relay(upstream, client, close_client, diag, request_id),
            status=upstream.status_code,
            headers=response_headers,
            direct_passthrough=True,
        )

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    app.add_url_rule("/api/v1", "proxy_api_v1_root", proxy_request, methods=methods)
    app.add_url_rule("/api/v1/<path:_ignored>", "proxy_api_v1", proxy_request, methods=methods)
    app.add_url_rule("/v1/<path:_ignored2>", "proxy_v1", proxy_request, methods=methods)
    app.add_url_rule("/responses", "proxy_responses", proxy_request, methods=methods)
    app.add_url_rule("/models", "proxy_models", proxy_request, methods=methods)

    if settings.startup_probe:
        def probe_worker() -> None:
            probe_holder["value"] = run_startup_probe(settings, diag)

        threading.Thread(target=probe_worker, name="bigmodel-startup-probe", daemon=True).start()
    return app


def _serve(settings: Settings) -> None:
    diag = DiagnosticLogger(settings)
    diag.emit(
        logging.INFO,
        "代理启动",
        "Codex 智谱诊断代理正在启动",
        version=APP_VERSION,
        python=sys.version.split()[0],
        platform=platform.platform(),
        settings=settings.public_dict(),
    )
    app = create_app(settings, diag)
    print()
    print("Codex 智谱诊断代理已启动")
    print(f"  控制台: http://{settings.bind_host}:{settings.bind_port}/")
    print(f"  Codex Base URL: http://{settings.bind_host}:{settings.bind_port}/api/v1")
    print(f"  中文日志: {Path(settings.log_dir).expanduser().resolve()}")
    print("  按 Ctrl+C 停止")
    print()
    from waitress import serve

    serve(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        threads=32,
        channel_timeout=max(120, int(settings.read_timeout_seconds or 600) + 60),
        cleanup_interval=15,
        clear_untrusted_proxy_headers=True,
    )


def _doctor(settings: Settings) -> int:
    diag = DiagnosticLogger(settings)
    diag.emit(
        logging.INFO,
        "手工网络诊断",
        "开始执行 DNS/TLS 诊断；不读取也不发送 API Key",
        version=APP_VERSION,
    )
    probe = run_startup_probe(settings, diag)
    output = create_diagnostic_zip(settings, probe)
    print(json.dumps(probe, ensure_ascii=False, indent=2))
    print(f"\n诊断包：{output}")
    tls_ok = probe.get("tls", {}).get("ok", True)
    http_ok = probe.get("http_via_configured_transport", {}).get("ok", True)
    return 0 if tls_ok and http_ok else 2


def _pack(settings: Settings) -> int:
    output = create_diagnostic_zip(settings)
    print(f"诊断包已生成：{output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex 智谱 Responses/SSE 本地诊断代理")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "doctor", "pack"),
        default="serve",
        help="serve=启动代理；doctor=只做网络诊断；pack=打包现有日志",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.command == "serve":
        _serve(settings)
        return 0
    if args.command == "doctor":
        return _doctor(settings)
    return _pack(settings)


if __name__ == "__main__":
    raise SystemExit(main())
