"""T5: FastAPI/httpx/sqlite3 自动埋点的采集前安全投影（Infrastructure 层）。

移植 N-Agent 已验证的自动埋点模式（P061 API 类型兼容代理、P057 安装前
源码核验、P058 精确枚举），并按 N-KB spec（spec-261029 Components 1/3）
收紧：

- ``instrument_app`` 在 create_app 返回前安装（middleware stack 尚未构建）；
  使用可延迟解析 Provider 的 API tracer（Provider 在 lifespan 最前段发布）。
  重复调用幂等；middleware stack 已构建后拒绝安装；失败不外溢。
- 全部安全投影在 SDK 接收前完成（写入阶段）：``_FilteringSpan`` 继承
  ``opentelemetry.trace.Span`` API 类型（isinstance 兼容），属性精确白名单
  放行、``db.statement`` 只投影表名、``record_exception``/``add_event``/
  异常路径逐项拦截、``set_status`` 剥离 description。批准的详情仅通过
  注入策略新鲜验证后写入；不放行原始 SQL/参数或异常对象。
- SERVER 仅现有注册业务路由（方法+模板白名单，模板来自真实路由表）；
  排除 /、health、static、MCP status、自检；禁用 ASGI receive/send span；
  未知路径不使用原 URL 命名。
- HTTPX/SQLite 仅在有效父操作下创建 CLIENT（不以 is_recording 判上下文
  有效性）；``suppress_client_spans`` 供 T7 显式抑制独立探测与 MCP status
  的依赖调用。
- HTTPX 完成 lifecycle：transport 返回时 span 尚未到真实终态（响应体未
  消费），``_SyncStream``/``_AsyncStream`` 在流读完/close/异常/取消时才
  结束 span，并在 span current 下各发一次 ``http_client_completed``
  Event 与 Log；不预读、不重复消费响应流。
- 入站 ``http_request_completed`` 每请求一次（方法、模板、状态、耗时、
  结果）；未采样仍保留；管理写请求即使降噪也记录访问日志；无响应取消
  状态为空。
- Qdrant/模型服务只承诺 REST HTTP transport span（不新增 gRPC/语义埋点）；
  exporter 自流量经 host 排除规则抑制。

只依赖标准库、OTel SDK/API/instrumentations、本包 logging/sanitizer；
禁止 import app.application / app.domain / app.interfaces（架构边界测试守护）。

告警（进程级副作用）：``install_global_instrumentors`` 注册进程级 httpx/
sqlite3 instrumentor（恰好一次）。测试进程中注册后不会卸载（wrapt 类级
包装无法安全解除）。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import httpx
from urllib.parse import urlparse

import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.trace import SpanKind, StatusCode

from . import logging as obs_logging
from .http_capture import Exchange, RequestStream, AsyncRequestStream
from .logging import HTTP_CLIENT_COMPLETED_EVENT, HTTP_COMPLETED_EVENT

__all__ = [
    "ALLOWED_AUTO_ATTRIBUTES",
    "instrument_app",
    "install_global_instrumentors",
    "suppress_client_spans",
    "global_registration_state",
]

# ---------------------------------------------------------------------------
# 采集前属性白名单（精确枚举，P058）：只在写入阶段放行这些键。
# ---------------------------------------------------------------------------

ALLOWED_AUTO_ATTRIBUTES = frozenset((
    # 入站 SERVER：方法/路由模板/状态/host/scheme/协议（无 url.path/query/头）
    "http.request.method", "http.method", "http.route",
    "http.response.status_code", "http.status_code",
    "http.scheme", "url.scheme", "http.flavor", "http.server_name",
    "net.host.name", "net.host.port",
    # 出站 CLIENT：host/port/模板/调用方/网络协议
    "net.peer.name", "net.peer.port",
    "server.address", "server.port",
    "url.template", "nkb.http.caller", "duration_ms",
    "network.protocol.name", "network.protocol.version",
    "network.peer.address", "network.peer.port",
    # SQLite：系统/库名/操作/表名（statement 一律拦截并投影表名）
    "db.system", "db.system.name", "db.name", "db.namespace",
    "db.operation", "db.collection.name", "db.sql.table",
))

# 不产生 SERVER span 的噪声路径（自检/健康/static/favicon/根重定向）。
_NOISE_TEMPLATES = frozenset((
    "/", "/health", "/health/dependencies", "/observations/otel", "/favicon.ico",
))

_WRITE_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))

# db.statement -> db.collection.name（表名投影，绝不保留 SQL 原文）。
_TABLE_PATTERN = re.compile(
    r"\b(?:FROM|INTO|UPDATE|TABLE)\s+[\"`\[]?([A-Za-z_][A-Za-z0-9_]{0,63})", re.I
)
# 单引号字符串字面量（含 '' 转义）：内容可能命中 FROM/INTO 等关键字，
# 先剥离再匹配，防止把字面量内容投影成表名（内容泄漏通道）。
_SQL_LITERAL_PATTERN = re.compile(r"'(?:[^']|'')*'")

# 全局注册状态（恰好一次；标志仅在成功后提交）。
_GLOBAL_LOCK = threading.Lock()
_GLOBAL_STATE = {"httpx": False, "sqlite3": False}

# CLIENT span 抑制（T7 注入点：独立探测与 MCP status 依赖调用）。
_SUPPRESS_CLIENT: ContextVar = ContextVar("nkb_otel_suppress_client", default=False)


def _extract_table_name(statement):
    if type(statement) is not str:
        return None
    try:
        stripped = _SQL_LITERAL_PATTERN.sub(" ", statement)
    except Exception:
        return None
    match = _TABLE_PATTERN.search(stripped)
    return match.group(1) if match else None


def _sanitize_attr_value(value):
    if type(value) is str:
        return obs_logging.sanitize_text(value)[:512]
    return value


def _filter_attributes(attributes):
    """采集前投影属性映射：白名单放行、statement 只留表名、其余丢弃。"""
    if attributes is None:
        return {}
    if isinstance(attributes, Mapping):
        items = attributes.items()
    else:
        items = attributes
    filtered = {}
    if isinstance(attributes, Mapping):
        filtered.update(obs_logging.project_flat_details(attributes))
    for key, value in items:
        if type(key) is not str:
            continue
        if key in ("db.statement", "db.query.text"):
            table = _extract_table_name(value) if type(value) is str else None
            if table:
                filtered["db.collection.name"] = table
            continue
        if key in ALLOWED_AUTO_ATTRIBUTES:
            filtered[key] = _sanitize_attr_value(value)
    return filtered


# ---------------------------------------------------------------------------
# API 类型兼容的 Span 代理（P061）：投影发生在 SDK 接收前
# ---------------------------------------------------------------------------


class _FilteringSpan(trace_api.Span):
    """包装真实 Span 的写入投影代理。

    - set_attribute(s)：精确白名单放行（字符串脱敏限长）；
      ``db.statement``/``db.query.text`` 只投影 ``db.collection.name``；
    - record_exception：拦截原始异常；add_event 仅放行精确登记事件，
      事实与详情均经过统一投影，内部 Prepared 写入保持相同元数据；
    - set_status：剥离 description（ExceptionHandlerMiddleware 会携带
      "Exception: 原文" 形式的 description）；
    - end：SERVER/CLIENT 完成事实（http_request_completed /
      http_client_completed）恰好一次，ID 取自当前 context。
    """

    def __init__(self, span, http_attributes=None, http_server=False, http_client=False,
                 detail_attributes=None):
        self._span = span
        self._http_attributes = http_attributes  # None 表示非 HTTP 埋点/被抑制
        self._http_server = http_server
        self._http_client = http_client
        self._lifecycle = None
        self._http_logged = False
        self._http_cancelled = False
        self._http_error = None
        self._completion_event_added = False
        self._http_capture = None
        self._http_details = {}
        # SDK attributes cannot be removed through the public API. Keep one
        # bounded safe detail buffer and publish it once, immediately at end.
        self._detail_attributes = {}
        self._detail_policy_token = obs_logging.detail_policy_token()
        self._details_unavailable = False
        if detail_attributes:
            self._update_details(detail_attributes)
        # CLIENT span 起始时点（供 http_client_completed 耗时计算）。
        self._http_started = time.perf_counter() if http_client else None

    def __getattr__(self, name):
        # 未投影的读路径（kind、context 等）委托底层 span；
        # _span 未初始化期间直接抛 AttributeError，避免递归。
        span = self.__dict__.get("_span")
        if span is None:
            raise AttributeError(name)
        return getattr(span, name)

    # -- 投影写路径 ---------------------------------------------------------

    def _update_details(self, attributes):
        if self._detail_policy_token != obs_logging.detail_policy_token():
            self._detail_attributes = obs_logging.project_flat_details(self._detail_attributes)
        projected = obs_logging.project_flat_details(attributes)
        if obs_logging.is_prepared_outlet(attributes):
            # The terminal Prepared owns the complete snapshot and its exact
            # metadata. Replace earlier direct writes instead of accumulating.
            self._detail_attributes = projected
        elif projected:
            merged = dict(self._detail_attributes)
            merged.update(projected)
            self._detail_attributes = obs_logging.fit_safe_flat_details(merged)
        elif any(type(key) is str and key.startswith("nkb.details.") for key in attributes):
            self._details_unavailable = True
        self._detail_policy_token = obs_logging.detail_policy_token()

    def _flush_details(self):
        if self._detail_policy_token != obs_logging.detail_policy_token():
            self._detail_attributes = obs_logging.project_flat_details(self._detail_attributes)
        try:
            if self._span.is_recording():
                if self._detail_attributes:
                    self._span.set_attributes(self._detail_attributes)
                if self._details_unavailable:
                    self._span.set_attribute("details_unavailable", True)
        finally:
            self._detail_attributes = {}

    def _set_projected(self, key, value):
        if key in ("db.statement", "db.query.text"):
            table = _extract_table_name(value) if type(value) is str else None
            if table:
                self._set_projected("db.collection.name", table)
            return
        if type(key) is str and key.startswith("nkb.details."):
            self._update_details({key:value})
            return
        if key not in ALLOWED_AUTO_ATTRIBUTES:
            return
        value = _sanitize_attr_value(value)
        try:
            self._span.set_attribute(key, value)
        except Exception:
            return
        if isinstance(self._http_attributes, dict):
            self._http_attributes[key] = value

    def set_attribute(self, key, value):
        self._set_projected(key, value)
        return self

    def set_attributes(self, attributes):
        if isinstance(attributes, Mapping):
            items = attributes.items()
        else:
            items = dict(attributes or {}).items()
        detail_source = attributes if isinstance(attributes, Mapping) else dict(items)
        self._update_details(detail_source)
        items = attributes.items() if isinstance(attributes, Mapping) else dict(attributes or {}).items()
        for key, value in items:
            if type(key) is str and key.startswith("nkb.details."):
                continue
            self._set_projected(key, value)
        return self

    def record_exception(self, exception, attributes=None, timestamp=None, escaped=False):
        # 异常对象（消息/堆栈）绝不写入 span 事件。
        return None

    def add_event(self, name, attributes=None, timestamp=None):
        if not obs_logging.is_registered_event(name):
            return self
        # 完成事件恰好一次：显式补投（_complete_*）与安全桥重注入
        # （SafeLogBridge._add_event_on_current_span）只允许首个生效。
        http = name in (HTTP_COMPLETED_EVENT, HTTP_CLIENT_COMPLETED_EVENT)
        if http and self._completion_event_added:
            return self
        if http:
            self._completion_event_added = True
        try:
            safe = obs_logging.project_event_attributes(attributes)
            if self._span.is_recording():
                self._span.add_event(name, safe)
        except Exception:
            pass
        return self

    def set_status(self, status, description=None):
        try:
            if isinstance(status, trace_api.Status):
                # 剥离 description（可能含异常原文）。
                self._span.set_status(trace_api.Status(status.status_code))
            else:
                self._span.set_status(status)
        except Exception:
            pass
        return self

    def update_name(self, name):
        try:
            self._span.update_name(name)
        except Exception:
            pass
        return self

    # -- 读路径与生命周期 ---------------------------------------------------

    def get_span_context(self):
        return self._span.get_span_context()

    def is_recording(self):
        try:
            return self._span.is_recording()
        except Exception:
            return False

    def end(self, end_time=None):
        if self._lifecycle is not None and not self._lifecycle.ready:
            # CLIENT 传输终态未到：推迟结束（流读完/close/异常时补）。
            self._lifecycle.end_requested = True
            return
        self._finish_completion()
        try:
            self._flush_details()
        except Exception:
            pass
        try:
            self._span.end(end_time)
        except Exception:
            pass

    def _finish_completion(self):
        if self._http_logged or not isinstance(self._http_attributes, dict):
            return
        self._http_logged = True
        try:
            if self._http_capture is not None:
                self._http_details = self._http_capture.project()
                self._http_capture = None
            if self._http_server:
                self._complete_http_server()
            elif self._http_client:
                self._complete_http_client()
        except Exception:
            pass

    def _outcome(self, status):
        if self._http_cancelled:
            return "cancelled"
        if self._lifecycle is not None and self._lifecycle.error is not None:
            return "error"
        if self._http_error is not None:
            return "error"
        if type(status) is int and status >= 400:
            return "error"
        # 传输已终态但未完整消费（如 close-without-read）→ incomplete
        if (
            self._lifecycle is not None
            and self._lifecycle.ready
            and not self._lifecycle.complete
        ):
            return "incomplete"
        return None

    def _complete_http_server(self):
        facts = self._http_attributes
        status = facts.get("http.response.status_code")
        outcome = self._outcome(status)
        if outcome is None:
            outcome = "completed" if status is not None else "incomplete"
        method = facts.get("http.request.method")
        route = facts.get("http.route")
        duration = facts.get("duration_ms")
        with trace_api.use_span(
            self, end_on_exit=False, record_exception=False,
            set_status_on_exception=False,
        ):
            obs_logging.log_http_request_completed(
                method, route, status, duration, outcome,
                details=self._http_details, owner_context=self,
            )
        self._fallback_http_event(HTTP_COMPLETED_EVENT, facts)

    def _fallback_http_event(self, event, facts):
        if not self._completion_event_added:
            flat = obs_logging._call('flatten', self._http_details)
            self.set_attributes(flat)
            self.add_event(event, dict(facts, **flat))

    def _complete_http_client(self):
        facts = self._http_attributes
        status = facts.get("http.response.status_code")
        outcome = self._outcome(status)
        if outcome is None:
            outcome = "completed" if status is not None else "incomplete"
        duration_ms = None
        if self._http_started is not None:
            try:
                duration_ms = round(
                    (time.perf_counter() - self._http_started) * 1000, 3
                )
                # 事件属性与日志出口都带耗时。
                self._set_projected("duration_ms", duration_ms)
            except Exception:
                duration_ms = None
        with trace_api.use_span(
            self, end_on_exit=False, record_exception=False,
            set_status_on_exception=False,
        ):
            obs_logging.log_http_client_completed(
                facts.get("http.request.method"),
                facts.get("server.address"),
                facts.get("server.port"),
                facts.get("url.template"),
                status,
                facts.get("nkb.http.caller"),
                duration_ms=duration_ms,
                outcome=outcome,
                details=self._http_details, owner_context=self,
            )
        self._fallback_http_event(HTTP_CLIENT_COMPLETED_EVENT, facts)


class _ClientLifecycle:
    """CLIENT span 的真实传输终态跟踪（once 保护）。"""

    __slots__ = ("span", "ready", "end_requested", "complete", "error", "_done")

    def __init__(self, span):
        self.span = span
        self.ready = False
        self.end_requested = False
        self.complete = False
        self.error = None
        self._done = False

    def finish(self, complete=False, error=None):
        if self._done:
            return
        self._done = True
        self.ready = True
        self.complete = complete
        self.error = error
        capture = self.span._http_capture
        if capture is not None:
            capture.response.complete = complete
        if error is not None:
            self.span._http_cancelled = isinstance(error, asyncio.CancelledError)
            self.span._http_error = None if self.span._http_cancelled else type(error).__name__
        if self.end_requested:
            try:
                self.span.end()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 响应流包装：真实传输终态（不预读、不重复消费）
# ---------------------------------------------------------------------------



class _SyncStream(httpx.SyncByteStream):
    """包装同步响应流：读完→完成、异常→错误、close→未完成（once）。"""

    def __init__(self, stream, lifecycle):
        self._stream = stream
        self._lifecycle = lifecycle

    def __iter__(self):
        try:
            for chunk in self._stream:
                if self._lifecycle.span._http_capture is not None:
                    self._lifecycle.span._http_capture.response.observe(chunk)
                yield chunk
        except BaseException as exc:  # 含 CancelledError；不吞 GeneratorExit 语义
            self._lifecycle.finish(error=exc)
            raise
        self._lifecycle.finish(complete=True)

    def close(self):
        try:
            close = getattr(self._stream, "close", None)
            if callable(close):
                close()
        except BaseException as exc:
            self._lifecycle.finish(error=exc)
            raise
        finally:
            self._lifecycle.finish()


class _AsyncStream(httpx.AsyncByteStream):
    """包装异步响应流：读完→完成、异常→错误、aclose→未完成（once）。"""

    def __init__(self, stream, lifecycle):
        self._stream = stream
        self._lifecycle = lifecycle

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                if self._lifecycle.span._http_capture is not None:
                    self._lifecycle.span._http_capture.response.observe(chunk)
                yield chunk
        except BaseException as exc:
            self._lifecycle.finish(error=exc)
            raise
        self._lifecycle.finish(complete=True)

    async def aclose(self):
        try:
            aclose = getattr(self._stream, "aclose", None)
            if callable(aclose):
                await aclose()
        except BaseException as exc:
            self._lifecycle.finish(error=exc)
            raise
        finally:
            self._lifecycle.finish()


def _sync_transport_wrapper(wrapped, instance, args, kwargs):
    request = args[0] if args else kwargs.get('request')
    original = request.stream if request is not None else None
    span = trace_api.get_current_span()
    lifecycle = None
    try:
        if isinstance(span, _FilteringSpan) and span._http_client and not span._http_logged and span._lifecycle is None:
            lifecycle = _ClientLifecycle(span)
            span._lifecycle = lifecycle
            if request is not None and obs_logging.details_capture_enabled():
                span._http_capture = Exchange(request=request)
                if not span._http_capture.request.complete:
                    request.stream = RequestStream(original, span._http_capture.request)
    except Exception:
        pass
    try:
        response = wrapped(*args, **kwargs)
        if lifecycle is not None:
            capture = span._http_capture
            if capture is not None:
                capture.response_headers(response.headers, decoded=response.is_stream_consumed)
            if response.is_stream_consumed:
                if capture is not None:
                    capture.response.observe(response.content)
                lifecycle.finish(complete=True)
            else:
                response.stream = _SyncStream(response.stream, lifecycle)
        return response
    except BaseException as exc:
        if lifecycle is not None:
            lifecycle.finish(error=exc)
        raise
    finally:
        if request is not None:
            request.stream = original


async def _async_transport_wrapper(wrapped, instance, args, kwargs):
    request = args[0] if args else kwargs.get('request')
    original = request.stream if request is not None else None
    span = trace_api.get_current_span()
    lifecycle = None
    try:
        if isinstance(span, _FilteringSpan) and span._http_client and not span._http_logged and span._lifecycle is None:
            lifecycle = _ClientLifecycle(span)
            span._lifecycle = lifecycle
            if request is not None and obs_logging.details_capture_enabled():
                span._http_capture = Exchange(request=request)
                if not span._http_capture.request.complete:
                    request.stream = AsyncRequestStream(original, span._http_capture.request)
    except Exception:
        pass
    try:
        response = await wrapped(*args, **kwargs)
        if lifecycle is not None:
            capture = span._http_capture
            if capture is not None:
                capture.response_headers(response.headers, decoded=response.is_stream_consumed)
            if response.is_stream_consumed:
                if capture is not None:
                    capture.response.observe(response.content)
                lifecycle.finish(complete=True)
            else:
                response.stream = _AsyncStream(response.stream, lifecycle)
        return response
    except BaseException as exc:
        if lifecycle is not None:
            lifecycle.finish(error=exc)
        raise
    finally:
        if request is not None:
            request.stream = original


def _wrap_httpx_transports():
    """类级 transport 包装（先于 HTTPXClientInstrumentor 注册 → 外层是埋点）。"""
    wrapt.wrap_function_wrapper(
        "httpx", "HTTPTransport.handle_request", _sync_transport_wrapper
    )
    wrapt.wrap_function_wrapper(
        "httpx", "AsyncHTTPTransport.handle_async_request", _async_transport_wrapper
    )


# ---------------------------------------------------------------------------
# 出站请求投影 hooks（0.66b1 签名：span, RequestInfo / ResponseInfo）
# ---------------------------------------------------------------------------


def _http_caller():
    """定位调用方 app.* 帧；只读模块名/函数名，绝不读参数值。"""
    try:
        frame = sys._getframe(1)
    except Exception:
        return None
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if (
            type(name) is str
            and name.startswith("app.")
            and not name.startswith("app.infrastructure.observability")
        ):
            return f"{name}:{frame.f_code.co_name}"
        frame = frame.f_back
    return None


def _request_info_fields(request):
    """从 RequestInfo 提取安全字段（方法/host/port/路径），不读头/query。"""
    method = request.method
    if type(method) is bytes:
        method = method.decode("ascii", errors="replace")
    url = request.url
    host = port = scheme = path = None
    try:
        if not isinstance(url, str):
            url = str(url)
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
        scheme = parsed.scheme or None
        path = parsed.path or "/"
        if port is None:
            port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    except Exception:
        pass
    return method, host, port, scheme, path


def _httpx_request_hook(span, request):
    if not isinstance(span, _FilteringSpan):
        return
    if not isinstance(span._http_attributes, dict):
        return  # 被抑制/无父级的 span：不投影
    method, host, port, scheme, path = _request_info_fields(request)
    facts = {
        "http.request.method": method,
        "server.address": host,
        "server.port": port,
        "url.scheme": scheme,
        "url.template": _http_route_template(path) if path else None,
        "nkb.http.caller": _http_caller(),
    }
    for key, value in facts.items():
        if value is not None:
            span.set_attribute(key, value)
    template = facts.get("url.template") or ""
    if host is not None:
        span.update_name(f"{method} {host} {template}".strip())


def _httpx_response_hook(span, request, response):
    if not isinstance(span, _FilteringSpan):
        return
    if not isinstance(span._http_attributes, dict):
        return
    status = getattr(response, "status_code", None)
    if type(status) is int:
        span.set_attribute("http.response.status_code", status)


async def _async_httpx_request_hook(span, request):
    _httpx_request_hook(span, request)


async def _async_httpx_response_hook(span, request, response):
    # 0.66b1 httpx 以 (span, request_info, response_info) 三参调用异步
    # response hook（无 try/except 保护，签名错误会中断业务调用）。
    _httpx_response_hook(span, request, response)


# ---------------------------------------------------------------------------
# 路由白名单（模板来自真实路由表）与出站路径模板
# ---------------------------------------------------------------------------


class _RouteAllowlist:
    """SERVER span 白名单：(method, template) 精确对 + MCP 前缀规则。"""

    __slots__ = ("_routes", "_mcp_path", "_excluded")

    def __init__(self, routes, mcp_path, excluded):
        self._routes = frozenset(routes)
        self._mcp_path = mcp_path
        self._excluded = frozenset(excluded)

    @property
    def mcp_path(self):
        return self._mcp_path

    @property
    def excluded(self):
        return self._excluded

    def allows(self, method, route):
        if type(method) is not str or type(route) is not str:
            return False
        if not route.startswith("/"):
            return False
        method = method.upper()
        if (method, route) in self._routes:
            return True
        if route in self._excluded:
            return False
        if self._mcp_path and (
            route == self._mcp_path or route.startswith(self._mcp_path + "/")
        ):
            return True
        return False


def _effective_routes(routes):
    """Use full include-router contexts, retaining prefixes and methods.

    FastAPI >=0.137 nests includes; >=0.137.2 exposes the public iterator.
    Older releases expose effective_route_contexts on include nodes, or
    already provide a flat route list. Do not flatten mounted ASGI apps.
    """
    from fastapi import routing

    iterator = getattr(routing, "iter_route_contexts", None)
    if callable(iterator):
        yield from iterator(routes)
        return
    for route in routes:
        effective = getattr(route, "effective_route_contexts", None)
        if callable(effective):
            yield from effective()
        else:
            yield route


def _derive_allowlist(app, settings):
    mcp_path = getattr(settings, "mcp_path", "/mcp") if settings is not None else "/mcp"
    if type(mcp_path) is not str or not mcp_path.startswith("/"):
        mcp_path = "/mcp"
    mcp_path = mcp_path.rstrip("/") or "/mcp"
    excluded = set(_NOISE_TEMPLATES)
    excluded.add(mcp_path + "/status")
    routes = set()
    templates = set()
    for route in _effective_routes(getattr(app, "routes", ()) or ()):
        path = getattr(route, "path", None)
        if type(path) is not str or not path.startswith("/") or path in excluded:
            continue
        methods = getattr(route, "methods", None)
        if not methods:
            continue  # Mount（MCP/static）走前缀规则，不进 methods 白名单
        for method in methods:
            if type(method) is str and method:
                routes.add((method.upper(), path))
        templates.add(path)
    allowlist = _RouteAllowlist(frozenset(routes), mcp_path, excluded)
    return allowlist, templates


def _server_identity(name, attributes):
    """从 span 名/属性提取 (method, route)；无模板（未知路径）返回 None。"""
    method = route = None
    if type(name) is str and " " in name:
        method, route = name.split(" ", 1)
    if isinstance(attributes, Mapping) and type(attributes.get("http.route")) is str:
        route = attributes["http.route"]
    return method, route


# 出站路径模板的保留词（结构性资源/动作名）。
_KEEP_SEGMENTS = frozenset((
    "api", "v1", "v2", "v3", "v4", "models", "embed", "embeddings", "completions",
    "collections", "points", "search", "documents", "index", "indexes", "jobs",
    "retrieval", "health", "dependencies", "observations", "otel", "metrics",
    "mcp", "status", "settings", "static", "admin", "private", "public",
    "content", "chunks", "tags", "sources", "upload", "files", "token", "auth",
    "stream", "results", "catalog", "query",
))
_CREDENTIAL_SEGMENT = re.compile(r"^[^/=]{1,64}=")
_SENSITIVE_SEGMENT = re.compile(
    r"(?i)(token|secret|password|passwd|credential|apikey|api_key|signature|"
    r"authorization|private[_-]?key)"
)
_ID_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_TEMPLATE_MAX_CHARS = 512


def _http_route_template(path):
    """出站 URL 路径 -> 安全模板：保留词原样、其余 {id}/{redacted}。"""
    if type(path) is not str or not path:
        return ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = []
    for segment in path.split("/"):
        if not segment:
            continue
        if segment in _KEEP_SEGMENTS:
            parts.append(segment)
        elif _CREDENTIAL_SEGMENT.match(segment) or _SENSITIVE_SEGMENT.search(segment):
            parts.append("{redacted}")
        elif _ID_SEGMENT.match(segment):
            parts.append("{id}")
        else:
            parts.append("{redacted}")
    template = "/" + "/".join(parts) if parts else "/"
    if len(template) > _TEMPLATE_MAX_CHARS:
        template = template[:_TEMPLATE_MAX_CHARS]
    return template


def _template_regex(template):
    parts = []
    for piece in re.split(r"(\{[^/{}]+\})", template):
        if piece.startswith("{") and piece.endswith("}"):
            parts.append(r"[^/?#]+")
        else:
            parts.append(re.escape(piece))
    return "".join(parts)


def _fastapi_excluded_urls(templates, mcp_path=None):
    """噪声路径的精确排除规则（正锚定，容忍尾斜杠与 query）。

    只收噪声路径（含 {mcp}/status 精确模板）；MCP 子树与业务路由
    不进排除表，由 tracer 白名单门控。
    """
    origin = r"^https?://[^/?#]+"
    paths = []
    for template in sorted(templates):
        paths.append(_template_regex(template) + r"/?(?:\?[^#]*)?$")
    if not paths:
        return r"^https?://[^/?#]+/.*"
    return ",".join(origin + p for p in paths)


# ---------------------------------------------------------------------------
# 延迟解析 Provider 的 API tracer（Provider 在 lifespan 最前段发布）
# ---------------------------------------------------------------------------


class _FilteringTracer:
    """API Tracer 代理：SERVER 白名单门控 + CLIENT 有效父级门控。"""

    def __init__(self, tracer, allowlist=None):
        self._tracer = tracer
        self._allowlist = allowlist

    def start_span(self, name, context=None, kind=SpanKind.INTERNAL,
                   attributes=None, links=None, start_time=None,
                   record_exception=False, set_status_on_exception=False):
        filtered = _filter_attributes(attributes)
        details = {key:value for key,value in filtered.items() if key.startswith("nkb.details.")}
        filtered = {key:value for key,value in filtered.items() if not key.startswith("nkb.details.")}
        if kind == SpanKind.SERVER and self._allowlist is not None:
            method, route = _server_identity(name, filtered)
            if not self._allowlist.allows(method, route):
                # 未知路径/方法：不建 span，绝不使用原 URL 命名。
                return _FilteringSpan(trace_api.INVALID_SPAN)
        if kind == SpanKind.CLIENT:
            if _SUPPRESS_CLIENT.get():
                return _FilteringSpan(trace_api.INVALID_SPAN)
            parent = trace_api.get_current_span(context).get_span_context()
            if not getattr(parent, "is_valid", False):
                # 无有效父操作：不建 CLIENT span（不用 is_recording 判断）。
                return _FilteringSpan(trace_api.INVALID_SPAN)
        span = self._tracer.start_span(
            name, context=context, kind=kind, attributes=filtered,
            links=links, start_time=start_time, record_exception=False,
            set_status_on_exception=False,
        )
        if kind == SpanKind.SERVER:
            facts = dict(filtered)
            if type(name) is str and " " in name:
                method, route = name.split(" ", 1)
                facts.setdefault("http.request.method", method)
                facts.setdefault("http.route", route)
            return _FilteringSpan(span, http_attributes=facts, http_server=True, detail_attributes=details)
        if kind == SpanKind.CLIENT:
            return _FilteringSpan(span, http_attributes=dict(filtered), http_client=True, detail_attributes=details)
        return _FilteringSpan(span, detail_attributes=details)

    @contextmanager
    def start_as_current_span(self, name, context=None, kind=SpanKind.INTERNAL,
                              attributes=None, links=None, start_time=None,
                              record_exception=False, set_status_on_exception=False,
                              end_on_exit=True):
        # 与 SDK 合同一致：默认在退出时结束 span；但先 end 后 detach，
        # 保证完成事实日志的 ID 取自该 span 仍 current 的 context。
        span = self.start_span(
            name, context=context, kind=kind, attributes=attributes,
            links=links, start_time=start_time, record_exception=False,
            set_status_on_exception=False,
        )
        cm = trace_api.use_span(
            span, end_on_exit=False, record_exception=False,
            set_status_on_exception=False,
        )
        cm.__enter__()
        try:
            yield span
        except BaseException as exc:
            if (
                isinstance(span, _FilteringSpan)
                and isinstance(span._http_attributes, dict)
            ):
                if isinstance(exc, asyncio.CancelledError):
                    span._http_cancelled = True
                else:
                    span._http_error = type(exc).__name__
            raise
        finally:
            if end_on_exit:
                span.end()
            cm.__exit__(None, None, None)


class _FilteringTracerProvider:
    """Provider 代理：所有 tracer 都经过 SERVER/CLIENT 门控。"""

    def __init__(self, provider, allowlist=None):
        self._provider = provider
        self._allowlist = allowlist

    def get_tracer(self, instrumenting_module_name, *args, **kwargs):
        return _FilteringTracer(
            self._provider.get_tracer(instrumenting_module_name, *args, **kwargs),
            self._allowlist,
        )

    def __getattr__(self, item):
        return getattr(self._provider, item)


_PROXY_PROVIDER_NAMES = ("ProxyTracerProvider",)


class _ProxyTracer:
    """延迟解析 tracer：lifespan 发布 Provider 后首个请求生效。"""

    def __init__(self, holder, instrumenting_module_name, *args, **kwargs):
        self._holder = holder
        self._name = instrumenting_module_name
        self._args = args
        self._kwargs = kwargs
        self._resolved = None

    def _tracer(self):
        if self._resolved is not None:
            return self._resolved
        provider = self._holder._resolve()
        if provider is None or type(provider).__name__ in _PROXY_PROVIDER_NAMES:
            return trace_api.NoOpTracer()
        self._resolved = _FilteringTracerProvider(
            provider, self._holder._allowlist
        ).get_tracer(self._name, *self._args, **self._kwargs)
        return self._resolved

    def start_span(self, *args, **kwargs):
        return self._tracer().start_span(*args, **kwargs)

    def start_as_current_span(self, *args, **kwargs):
        return self._tracer().start_as_current_span(*args, **kwargs)


class _ProxyTracerProvider:
    """可延迟解析的 Provider：显式传入优先，否则解析全局发布值。"""

    def __init__(self, provider=None, allowlist=None):
        self._explicit = provider
        self._allowlist = allowlist

    def _resolve(self):
        if self._explicit is not None:
            return self._explicit
        try:
            return trace_api.get_tracer_provider()
        except Exception:
            return None

    def get_tracer(self, instrumenting_module_name, *args, **kwargs):
        return _ProxyTracer(self, instrumenting_module_name, *args, **kwargs)


@contextmanager
def suppress_client_spans():
    """显式抑制 CLIENT span（T7：独立探测、MCP status 依赖调用）。"""
    token = _SUPPRESS_CLIENT.set(True)
    try:
        yield
    finally:
        _SUPPRESS_CLIENT.reset(token)


# ---------------------------------------------------------------------------
# 入站访问事实中间件（位于 OTel ASGI 埋点内侧）
# ---------------------------------------------------------------------------


class _ObserveSend:
    """Tap response messages; defer SDK end until final send succeeds.

    OTel ASGI may request end inside send. The lifecycle keeps the span open
    until this observer sees the actual result, preserving body and outcome.
    """

    __slots__ = ("_send", "_span", "_traced", "_started", "status", "final")

    def __init__(self, send, span=None, traced=False, started=0.0):
        self._send = send
        self._span = span
        self._traced = traced
        self._started = started
        self.status = None
        self.final = False

    async def __call__(self, message):
        mtype = message.get("type")
        if mtype == "http.response.start":
            if self._traced and self._span._http_capture is not None:
                headers = {k.decode('latin1'):v.decode('latin1') for k,v in message.get('headers', [])}
                self._span._http_capture.response_headers(headers)
            status = message.get("status")
            if type(status) is int:
                self.status = status
                if self._traced:
                    # otel_send 转发后可能立即 end；状态码先行入 facts。
                    self._span._set_projected("http.response.status_code", status)
                    # SERVER 5xx 为 ERROR；4xx 保持默认 UNSET（客户端输入
                    # 错误不映射为服务端故障）。description 一律不带。
                    if status >= 500:
                        self._span.set_status(trace_api.Status(StatusCode.ERROR))
        elif mtype == 'http.response.body' and self._traced:
            if self._span._http_capture is not None:
                self._span._http_capture.response.observe(message.get('body', b''))
        await self._send(message)
        if mtype == 'http.response.body' and not message.get('more_body', False):
            self.final = True
            if self._traced:
                self._span._set_projected('duration_ms', round((time.perf_counter()-self._started)*1000, 3))
                if self._span._lifecycle is not None:
                    self._span._lifecycle.finish(complete=True)


class _HttpAccessFactsMiddleware:
    """入站完成事实与降噪访问日志。

    - traced（SERVER span 在白名单内）：状态码/耗时在 send 时点写入
      facts；无最终 body（异常/取消）时在 SERVER 仍 current 时补 end
      （触发恰好一次 http_request_completed，取消状态为空）；
    - untraced（被抑制）：仅写方法（POST/PUT/PATCH/DELETE）且非噪声路径
      记录访问日志（路由模板，无原文）。
    """

    def __init__(self, app, mcp_path="/mcp"):
        self.app = app
        self._mcp_path = (mcp_path or "/mcp").rstrip("/")

    def _is_noise(self, path):
        if path in _NOISE_TEMPLATES:
            return True
        if path == "/static" or path.startswith("/static/"):
            return True
        if self._mcp_path and path == self._mcp_path + "/status":
            return True
        return False

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = (scope.get("method") or "").upper()
        path = scope.get("path") or ""
        started = time.perf_counter()
        span = trace_api.get_current_span()
        traced = (
            isinstance(span, _FilteringSpan)
            and isinstance(span._http_attributes, dict)
            and span._http_server
        )
        cancelled = False
        errored = False
        if traced:
            span._lifecycle = _ClientLifecycle(span)
            if obs_logging.details_capture_enabled():
                try:
                    span._http_capture = Exchange(scope=scope)
                except Exception:
                    span._details_unavailable = True
        original_receive = receive

        async def observed_receive():
            message = await original_receive()
            if traced and span._http_capture is not None and message.get('type') == 'http.request':
                body = span._http_capture.request
                body.observe(message.get('body', b''))
                body.complete = not message.get('more_body', False)
            return message

        send_obs = _ObserveSend(send, span, traced, started)
        try:
            await self.app(scope, observed_receive, send_obs)
        except asyncio.CancelledError:
            cancelled = True
            if traced:
                span._http_cancelled = True
            raise
        except BaseException as exc:
            errored = True
            if traced:
                span._http_error = type(exc).__name__
                span.set_status(trace_api.Status(StatusCode.ERROR))
            raise
        finally:
            try:
                if traced:
                    if not send_obs.final:
                        # otel_send 未触发 end（异常/取消/无响应）：
                        # SERVER 仍 current，补全事实并结束。
                        if send_obs.status is None and errored and not cancelled:
                            span._set_projected("http.response.status_code", 500)
                            # 无响应的异常路径等价 5xx：SERVER 标记 ERROR。
                            span.set_status(trace_api.Status(StatusCode.ERROR))
                        span._set_projected(
                            "duration_ms",
                            round((time.perf_counter() - started) * 1000, 3),
                        )
                        span._lifecycle.finish()
                        if not span._http_logged:
                            span.end()
                elif method in _WRITE_METHODS and not self._is_noise(path):
                    status = send_obs.status
                    duration_ms = round((time.perf_counter() - started) * 1000, 3)
                    outcome = (
                        "cancelled" if cancelled
                        else ("completed" if status is not None else "incomplete")
                    )
                    obs_logging.log_http_request_completed(
                        method, _http_route_template(path), status,
                        duration_ms, outcome,
                    )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 安装入口
# ---------------------------------------------------------------------------


def instrument_app(app, *, settings=None, tracer_provider=None):
    """在 create_app 返回前安装 FastAPI 埋点与访问事实中间件。

    幂等；middleware stack 已构建后拒绝；任何失败返回 False 不外溢。
    """
    try:
        return _instrument_app_impl(app, settings, tracer_provider)
    except Exception:
        return False


def _instrument_app_impl(app, settings, tracer_provider):
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        return True
    if getattr(app, "middleware_stack", None) is not None:
        return False
    allowlist, templates = _derive_allowlist(app, settings)
    provider = _ProxyTracerProvider(tracer_provider, allowlist=allowlist)
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # P057 已核验 0.66b1 签名：无 request/response_hook、无 enable_commenter。
    # 排除规则只覆盖噪声路径；业务路由由 tracer 白名单门控，禁止进排除表。
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=_fastapi_excluded_urls(allowlist.excluded, allowlist.mcp_path),
        exclude_spans=["receive", "send"],
    )
    app.add_middleware(_HttpAccessFactsMiddleware, mcp_path=allowlist.mcp_path)
    return True


def _set_httpx_self_traffic_exclusion(settings=None):
    """把 exporter/ profiler 端点 host 加入 httpx 排除规则（保留既有项）。"""
    endpoints = []
    if settings is not None:
        for field in ("otel_exporter_otlp_endpoint", "profiling_pyroscope_endpoint"):
            value = getattr(settings, field, None)
            if type(value) is str and value.strip():
                endpoints.append(value.strip())
    patterns = []
    for endpoint in endpoints:
        try:
            netloc = urlparse(endpoint).netloc
        except Exception:
            continue
        if netloc:
            patterns.append(r"https?://" + re.escape(netloc) + r"(?:[/#?]|$)")
    if not patterns:
        return
    existing = os.environ.get("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", "")
    merged = [p for p in existing.split(",") if p]
    for pattern in patterns:
        if pattern not in merged:
            merged.append(pattern)
    os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"] = ",".join(merged)


def install_global_instrumentors(*, settings=None, tracer_provider=None):
    """注册进程级 httpx/sqlite3 instrumentor（恰好一次，成功后才提交标志）。

    在 lifespan 最前段、业务 client/connection 创建之前调用。
    """
    with _GLOBAL_LOCK:
        try:
            if not (_GLOBAL_STATE["httpx"] and _GLOBAL_STATE["sqlite3"]):
                provider = _ProxyTracerProvider(tracer_provider, allowlist=None)
                _set_httpx_self_traffic_exclusion(settings)
            if not _GLOBAL_STATE["httpx"]:
                from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

                _wrap_httpx_transports()
                HTTPXClientInstrumentor().instrument(
                    tracer_provider=provider,
                    request_hook=_httpx_request_hook,
                    response_hook=_httpx_response_hook,
                    async_request_hook=_async_httpx_request_hook,
                    async_response_hook=_async_httpx_response_hook,
                )
                _GLOBAL_STATE["httpx"] = True
            if not _GLOBAL_STATE["sqlite3"]:
                from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor

                SQLite3Instrumentor().instrument(tracer_provider=provider)
                _GLOBAL_STATE["sqlite3"] = True
            return True
        except Exception:
            return False


def global_registration_state():
    """全局 instrumentor 注册状态（T7 自检消费）。"""
    return {"httpx": _GLOBAL_STATE["httpx"], "sqlite3": _GLOBAL_STATE["sqlite3"]}


_SYSTEM_METRICS_REGISTERED = False


def instrument_system_metrics(meter_provider=None):
    """Lazy, exactly-once system metrics on the existing meter pipeline."""
    global _SYSTEM_METRICS_REGISTERED
    try:
        with _GLOBAL_LOCK:
            if _SYSTEM_METRICS_REGISTERED:
                return True
            from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
            SystemMetricsInstrumentor().instrument(meter_provider=meter_provider)
            _SYSTEM_METRICS_REGISTERED = True
        return True
    except Exception:
        return False


def _reset_for_tests():
    """仅重置注册标志（wrapt 类级包装与 SDK 全局状态不回滚）。"""
    with _GLOBAL_LOCK:
        global _SYSTEM_METRICS_REGISTERED
        _SYSTEM_METRICS_REGISTERED = False
        _GLOBAL_STATE["httpx"] = False
        _GLOBAL_STATE["sqlite3"] = False
