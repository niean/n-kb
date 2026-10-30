"""py-spy profile 采样与 Pyroscope 摄取（Infrastructure 层，T6 of plan-261029）。

移植 N-Agent 已验证的单 worker 串行窗口模式
（N-Agent app/infrastructure/observability/profiling.py），按 spec-261029
调整为固定 10 秒窗口 / 100 Hz：

- 目标只为本进程 Python PID；py-spy 命令行固定为
  ``py-spy record --pid <self> --format raw --rate 100 --duration 10
  --output <tmp>``，绝不传 ``--locals``（不采集变量值）。
- 窗口固定 10 秒、频率 100 Hz；一个窗口结束（含上传）后才允许下一个
  窗口开始，窗口不重叠、不做积压补采；窗口之间的等待取
  ``profiling_interval_seconds``（10..300，可被 shutdown 中断）。
- 私有临时文件位于系统临时目录（绝不在 workspace）：目录 0700、文件
  0600；16 MiB 硬上限在采样过程中强制执行（超限立即终止子进程并标
  incomplete），不是事后检查。
- 原始 folded 栈投影为安全帧（函数名 + 相对源位置）：动态代码文件名、
  线程名、异常采样段丢弃，workspace/用户目录前缀剥离；无法识别的段
  一律丢弃（T3 同款 fail-closed 纪律）。全部帧被丢弃时报告安全错误码
  ``empty_profile``，原始文件绝不上传。
- 上传走 Pyroscope ``/ingest``，沿用 spike 核验过的合同：
  ``name=<服务名>&format=folded&units=samples&aggregationType=sum&
  sampleRate=100&from=<unix>&until=<unix>``，超时 5 秒。身份与 Trace
  资源同源（``SERVICE_NAME``/版本/每进程 instance.id 均来自 setup 模块
  与组合根注入的同一 handle）；该合同只有 ``name`` 参数，version/
  instance 一致性由同源保证，不虚构未核验参数。
- 失败（attach 失败、非零退出、超限、watchdog、非 2xx 摄取响应）丢弃
  窗口并退避 10s、30s；连续第三次失败停用本进程采样器并报告 degraded；
  成功窗口清零失败计数。临时文件成败都清理；任意时刻最多一个窗口在
  途；stderr 有界读取且绝不原文进入状态。
- watchdog：窗口 + 5 秒。shutdown：terminate -> kill/wait 2 秒内回收，
  经 ``register_stop_callback`` 计入观测句柄整体 5 秒预算。
- 缺少 py-spy 二进制是有界的 degraded Profile 信号，绝不是应用失败。
- 本模块绝不写 stdout（MCP stdio 合同）：子进程输出全部进临时文件，
  诊断只走 logging（stderr 管道）与状态快照。

只 import ``app.config`` 与本包模块 + stdlib/httpx，绝不 import
application/domain/interfaces 模块。
"""
from __future__ import annotations

import os
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.config import Settings
from app.infrastructure.observability.setup import SERVICE_NAME

# spike 核验过的摄取合同（N-Agent T9 S1, 2026-10-24, py-spy 0.4.2 /
# grafana/pyroscope:1.21.1）：POST /ingest + folded 栈、samples 单位、
# sum 聚合；查询用 {service_name="..."} 选择器。
INGEST_FORMAT = "folded"
INGEST_UNITS = "samples"
INGEST_AGGREGATION_TYPE = "sum"
INGEST_SAMPLE_RATE = 100

# spec-261029：窗口固定 10 秒、频率 100 Hz、一次上传超时 5 秒。
WINDOW_SECONDS = 10
UPLOAD_TIMEOUT_SECONDS = 5.0
WATCHDOG_SLACK_SECONDS = 5.0
STOP_BUDGET_SECONDS = 2.0
MAX_PROFILE_BYTES = 16 * 1024 * 1024  # 16 MiB，采样过程中强制执行
MAX_STDERR_BYTES = 4096  # 有界 stderr 读取；绝不原文投影进状态
MAX_FILENAME_CHARS = 256
PYSPY_RATE = 100
TERMINATE_GRACE_SECONDS = 0.5

# 连续失败退避：第一次 10s、第二次 30s、第三次停用。
BACKOFF_SCHEDULE: tuple = (10.0, 30.0)
MAX_CONSECUTIVE_FAILURES = 3

# 特殊窗口结果：因 shutdown 请求中止采样；既非成功也非失败
# （采样器无论哪种都会退出）。
_SHUTDOWN_ABORTED = "__shutdown_aborted__"

_AUTO_RESOLVE_BINARY = object()

_FRAME_RE = re.compile(r"^(?P<func>.+) \((?P<file>[^()]*):(?P<line>\d+)\)$")
# 动态代码文件名（<string>、<frozen ...>、eval/exec 目标）可携带任意
# 文本（潜在秘密）-> 绝不投影。
_DYNAMIC_FILENAME_RE = re.compile(r"<[^>]*>")


# ---------------------------------------------------------------------------
# folded 栈安全投影
# ---------------------------------------------------------------------------


def _strip_prefixes(filename: str, prefixes: Sequence[str]) -> str:
    for prefix in prefixes:
        if not prefix:
            continue
        normalized = str(prefix).rstrip("/") + "/"
        if filename.startswith(normalized):
            stripped = filename[len(normalized):]
            if stripped:
                return stripped
    return filename


def project_folded_stacks(
    text: str,
    *,
    strip_prefixes: Sequence[str] = (),
    max_line_chars: int = 2048,
    max_output_bytes: int = MAX_PROFILE_BYTES,
) -> str:
    """把原始 py-spy folded 栈投影为仅含安全帧的 folded 文本。

    一个段只有在匹配 ``func (path:line)`` 且文件名非动态代码时才保留；
    其余（线程名、GIL/异常采样标记、动态代码帧）一律丢弃，对应 spec
    的"无法投影则丢弃"而非前缀改写。结果限单行长度与总字节数；没有任何
    安全帧存活时返回空串。"""
    out: List[str] = []
    total = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, count = line.rpartition(" ")
        if not sep or not count.isdigit():
            continue
        kept: List[str] = []
        for segment in head.split(";"):
            segment = segment.strip()
            if not segment:
                continue
            match = _FRAME_RE.match(segment)
            if match is None:
                continue
            filename = match.group("file").strip()
            func = match.group("func").strip()
            if not func or not filename:
                continue
            if _DYNAMIC_FILENAME_RE.search(filename):
                continue
            if any(ord(char) < 32 or char == ";" for char in filename + func):
                continue
            filename = _strip_prefixes(filename, strip_prefixes)[
                :MAX_FILENAME_CHARS
            ]
            if not filename:
                continue
            kept.append(f"{func} ({filename}:{match.group('line')})")
        if not kept:
            continue
        out_line = ";".join(kept) + " " + count
        if len(out_line) > max_line_chars:
            continue
        # 每行一次 UTF-8 编码（最高 16 MiB 的热路径）。
        encoded = (out_line + "\n").encode("utf-8")
        if total + len(encoded) > max_output_bytes:
            break
        total += len(encoded)
        out.append(out_line)
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------------------
# 默认子进程 / 上传适配器
# ---------------------------------------------------------------------------


def _popen_factory(
    command: List[str], *, stdout: Any, stderr: Any
) -> subprocess.Popen:
    return subprocess.Popen(command, stdout=stdout, stderr=stderr)


class _HttpxUploader:
    """Pyroscope /ingest 上传器，复用 instrumentors.py 的 httpx
    自流量排除（单一机制，不建第二套）。"""

    def __init__(self, pyroscope_endpoint: str):
        self._endpoint = pyroscope_endpoint
        # 即使本上传器先于 instrument_app 安装构造，也把摄取 URL 排除出
        # httpx 自动 span（幂等；保留既有模式）。传入真实 Settings 形状，
        # 延迟 import 使禁用状态下 import profiling 不加载埋点模块。
        try:
            shim = Settings(
                _env_file=None,
                otel_exporter_otlp_endpoint="",
                profiling_pyroscope_endpoint=pyroscope_endpoint,
            )
        except Exception:
            return
        try:
            from app.infrastructure.observability.instrumentors import (
                _set_httpx_self_traffic_exclusion,
            )

            _set_httpx_self_traffic_exclusion(shim)
        except Exception:
            pass

    def upload(
        self,
        *,
        endpoint: str,
        params: Dict[str, Any],
        content: bytes,
        timeout: float,
    ) -> int:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, params=params, content=content)
            return response.status_code


# ---------------------------------------------------------------------------
# 采样器
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class ProfilingSampler:
    """单 PID 的串行 py-spy 窗口循环 + Pyroscope 摄取。

    全部副作用（子进程、上传、睡眠、时钟）均可注入，测试用假件运行，
    不产生真实 py-spy 流量，也不要求 CI 提权。"""

    def __init__(
        self,
        *,
        instance_id: str,
        pyroscope_endpoint: str,
        interval_seconds: int,
        pyspy_binary: Any = _AUTO_RESOLVE_BINARY,
        target_pid: Optional[int] = None,
        service_name: str = SERVICE_NAME,
        uploader: Any = None,
        subprocess_factory: Any = None,
        clock: Callable[[], float] = time.monotonic,
        epoch_clock: Callable[[], float] = time.time,
        sleeper: Optional[Callable[[float], None]] = None,
        poll_interval: float = 0.5,
        temp_root: Optional[str] = None,
        strip_prefixes: Optional[Sequence[str]] = None,
        on_status: Optional[Callable[[str, Optional[str]], None]] = None,
    ):
        self.instance_id = instance_id
        self.pyroscope_endpoint = pyroscope_endpoint
        self.interval_seconds = int(interval_seconds)
        self.service_name = service_name
        self.target_pid = target_pid if target_pid is not None else os.getpid()
        # 一次性解析（不逐窗口）：workspace/用户目录前缀从投影文件名剥离。
        self._strip_prefixes = (
            tuple(strip_prefixes)
            if strip_prefixes is not None
            else (os.getcwd(), str(Path.home()))
        )
        self._pyspy_requested = pyspy_binary
        self._uploader = uploader
        self._factory = (
            subprocess_factory if subprocess_factory is not None else _popen_factory
        )
        self._clock = clock
        self._epoch_clock = epoch_clock
        self._sleeper = sleeper if sleeper is not None else self._default_sleeper
        self._poll_interval = poll_interval
        self._temp_root = temp_root
        self._on_status = on_status

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self._proc_lock = threading.Lock()
        self._proc: Any = None
        self._started = False

        self._lock = threading.Lock()
        self._current_error: Optional[str] = None
        self._consecutive_failures = 0
        self._total_failures = 0
        self._windows_attempted = 0
        self._windows_completed = 0
        # 因 16 MiB 超限或 watchdog 被截断的窗口数（spec：标 incomplete）。
        self._windows_incomplete = 0
        self._last_success: Optional[str] = None
        self._last_failure: Optional[str] = None
        self._disabled = False
        self._diagnostic = ""

    # -- 状态 ---------------------------------------------------------------

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def current_error(self) -> Optional[str]:
        return self._current_error

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def windows_attempted(self) -> int:
        return self._windows_attempted

    @property
    def windows_completed(self) -> int:
        return self._windows_completed

    @property
    def windows_incomplete(self) -> int:
        return self._windows_incomplete

    @property
    def status(self) -> str:
        if self._current_error or self._disabled:
            return "degraded"
        return "healthy"

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status_snapshot(self) -> Dict[str, Any]:
        """纯 JSON、有界状态（仅安全错误码，绝不携带原始 stderr）。"""
        with self._lock:
            return {
                "enabled": True,
                "status": self.status,
                "disabled": self._disabled,
                "instance_id": self.instance_id,
                "target_pid": self.target_pid,
                "service_name": self.service_name,
                "interval_seconds": self.interval_seconds,
                "window_seconds": WINDOW_SECONDS,
                "sample_rate": PYSPY_RATE,
                "current_error": self._current_error,
                "consecutive_failures": self._consecutive_failures,
                "total_failures": self._total_failures,
                "windows_attempted": self._windows_attempted,
                "windows_completed": self._windows_completed,
                "windows_incomplete": self._windows_incomplete,
                "last_success": self._last_success,
                "last_failure": self._last_failure,
            }

    # -- 生命周期 -------------------------------------------------------------

    def _resolve_binary(self) -> Optional[str]:
        if self._pyspy_requested is _AUTO_RESOLVE_BINARY:
            try:
                return shutil.which("py-spy")
            except Exception:
                return None
        return self._pyspy_requested or None

    def start(self) -> bool:
        """启动串行采样循环。

        py-spy 不可用、worker 线程无法启动或本采样器已停止时返回 False
        （degraded Profile 信号，绝不向应用抛异常）；``stop()`` 之后不
        支持重启——请通过 ``get_or_create_profiler`` 新建采样器。"""
        if self._stopped:
            return False
        if self._started:
            return True
        if self._resolve_binary() is None:
            self._set_error("pyspy_missing")
            return False
        self._started = True
        self._thread = threading.Thread(
            target=self._loop, name="n-kb-profiling-sampler", daemon=True
        )
        try:
            self._thread.start()
        except Exception:
            # 例如 RuntimeError("can't start new thread")：degraded 信号，
            # 绝不向应用抛出。
            self._started = False
            self._thread = None
            self._set_error("sampler_error")
            return False
        return True

    def register_with_handle(self, handle: Any) -> bool:
        """把 Profile 回收（terminate -> kill/wait 2 秒内）注册为观测句柄
        的 stop callback；2 秒计入句柄整体 shutdown 预算。须在 shutdown
        前尽早注册（句柄 shutdown 后注册返回 False）。"""
        return bool(handle.register_stop_callback(self.stop))

    def _safe_now(self) -> float:
        """注入时钟 + monotonic 兜底：``stop()`` 的子进程回收绝不能因
        时钟故障而失败。"""
        try:
            return self._clock()
        except Exception:
            return time.monotonic()

    def stop(self, timeout: float = STOP_BUDGET_SECONDS) -> bool:
        """停止采样并在 ``timeout``（默认 2 秒）内回收子进程：
        terminate -> kill -> wait，随后 join 循环线程。"""
        self._stop_event.set()
        self._stopped = True
        deadline = self._safe_now() + timeout
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(
                    timeout=min(
                        TERMINATE_GRACE_SECONDS,
                        max(0.0, deadline - self._safe_now()),
                    )
                )
            except Exception:
                pass
            if getattr(proc, "poll", lambda: None)() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=max(0.0, deadline - self._safe_now()))
                except Exception:
                    pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, deadline - self._safe_now()))
        return not self.is_running()

    def _default_sleeper(self, seconds: float) -> None:
        # 可中断退避：shutdown 会提前打断等待。
        self._stop_event.wait(seconds)

    # -- 窗口循环 ---------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                code = self._run_window()
            except Exception:
                # 顶层守卫：窗口内的意外失败（如临时目录创建抛错）必须让
                # 信号 degraded，而不是 worker 线程静默死亡、状态仍 healthy。
                code = "sampler_error"
            if code == "":
                self._record_success()
                # 下一窗口在本窗口（含上传）完成后再开始：等待间隔可被
                # shutdown 中断，窗口不重叠。
                self._sleeper(self.interval_seconds)
            elif code == _SHUTDOWN_ABORTED:
                break
            else:
                self._record_failure(code)
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self._disable()
                    break
                self._sleeper(
                    BACKOFF_SCHEDULE[
                        min(self._consecutive_failures, len(BACKOFF_SCHEDULE)) - 1
                    ]
                )

    def _build_command(self, output_path: str) -> List[str]:
        return [
            "py-spy",
            "record",
            "--pid",
            str(self.target_pid),
            "--format",
            "raw",
            "--rate",
            str(PYSPY_RATE),
            "--duration",
            str(WINDOW_SECONDS),
            "--output",
            output_path,
        ]

    def _run_window(self) -> str:
        """运行一个采样窗口；成功返回 ""，失败返回安全错误码，shutdown
        打断返回 _SHUTDOWN_ABORTED。"""
        self._diagnostic = ""
        try:
            tmp_dir = tempfile.mkdtemp(
                prefix="n-kb-profiling-", dir=self._temp_root
            )
            # mkdtemp 本身即 0700；chmod 保证任意 umask 下确定性。下方文件
            # 经 os.open 以 0600 创建。
            os.chmod(tmp_dir, 0o700)
        except Exception as exc:
            self._diagnostic = str(exc)
            return "temp_failed"
        raw_path = os.path.join(tmp_dir, "raw.folded")
        err_path = os.path.join(tmp_dir, "pyspy.stderr")
        # 逐资源开/关：第二个文件失败不得泄漏第一个的 fd。
        raw_f: Optional[Any] = None
        err_f: Optional[Any] = None
        try:
            raw_fd = os.open(raw_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                raw_f = os.fdopen(raw_fd, "w+b")
            except Exception:
                os.close(raw_fd)
                raise
            err_fd = os.open(err_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                err_f = os.fdopen(err_fd, "w+b")
            except Exception:
                os.close(err_fd)
                raise
        except Exception:
            for handle in (raw_f, err_f):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
            self._cleanup(tmp_dir)
            return "temp_failed"
        try:
            command = self._build_command(raw_path)
            try:
                with self._proc_lock:
                    proc = self._factory(command, stdout=raw_f, stderr=err_f)
                    self._proc = proc
            except Exception as exc:
                self._diagnostic = str(exc)
                return "attach_failed"
            self._windows_attempted += 1
            outcome = self._wait_for_process(proc, raw_f)
            if outcome == _SHUTDOWN_ABORTED:
                return _SHUTDOWN_ABORTED
            if outcome is not None:
                if outcome in ("profile_too_large", "watchdog_timeout"):
                    # 采样被截断：标 incomplete，窗口不计成功。
                    with self._lock:
                        self._windows_incomplete += 1
                return outcome  # profile_too_large / watchdog_timeout
            return self._collect_and_upload(proc, raw_f, err_f)
        finally:
            # 异常逃逸路径也回收子进程，不留孤儿 py-spy（stop 不再持有引用）。
            try:
                if proc is not None and proc.poll() is None:
                    self._terminate_and_kill(proc)
            except Exception:
                pass
            with self._proc_lock:
                self._proc = None
            for handle in (raw_f, err_f):
                try:
                    handle.close()
                except Exception:
                    pass
            # 成功与失败都清理；任意时刻最多存在一个窗口的临时文件。
            self._cleanup(tmp_dir)

    def _wait_for_process(self, proc: Any, raw_f: Any) -> Optional[str]:
        """轮询子进程直至退出；采样过程中强制 16 MiB 上限与窗口+5 秒
        watchdog。返回错误码，子进程自行退出返回 None。"""
        start = self._safe_now()
        deadline = start + WINDOW_SECONDS + WATCHDOG_SLACK_SECONDS
        while True:
            if self._stop_event.is_set():
                self._terminate_and_kill(proc)
                return _SHUTDOWN_ABORTED
            rc = proc.poll()
            if rc is not None:
                return None
            try:
                size = os.fstat(raw_f.fileno()).st_size
            except Exception:
                size = 0
            if size > MAX_PROFILE_BYTES:
                self._terminate_and_kill(proc)
                return "profile_too_large"
            if self._safe_now() >= deadline:
                self._terminate_and_kill(proc)
                return "watchdog_timeout"
            # 轮询等待可被 shutdown 打断，且不计入退避睡眠。
            self._stop_event.wait(self._poll_interval)

    @staticmethod
    def _terminate_and_kill(proc: Any) -> None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        except Exception:
            pass

    @staticmethod
    def _read_bounded(fileobj: Any, limit: int = MAX_STDERR_BYTES) -> bytes:
        try:
            fileobj.seek(0)
            return fileobj.read(limit)
        except Exception:
            return b""

    @staticmethod
    def _cleanup(tmp_dir: str) -> None:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def _classify_exit(self, stderr_head: bytes, rc: int) -> str:
        lowered = stderr_head.lower()
        attach_markers = (
            b"failed to attach",
            b"could not attach",
            b"permission",
            b"not permitted",
            b"ptrace",
        )
        if any(marker in lowered for marker in attach_markers):
            return "attach_failed"
        return "subprocess_failed"

    def _collect_and_upload(
        self, proc: Any, raw_f: Any, err_f: Any
    ) -> str:
        # TOCTOU 复查：被 stop 终止的窗口绝不能误判为失败，也绝不能上传
        # 半份 profile（子进程收到 SIGTERM 可能以 0 退出）。
        if self._stop_event.is_set():
            return _SHUTDOWN_ABORTED
        rc = proc.poll()
        if rc != 0:
            stderr = self._read_bounded(err_f)
            self._diagnostic = stderr.decode("utf-8", errors="replace")
            return self._classify_exit(stderr, rc)
        # 投影失败绝不回退为上传原始文件。
        try:
            raw_f.seek(0)
            raw_bytes = raw_f.read(MAX_PROFILE_BYTES + 1)
            if len(raw_bytes) > MAX_PROFILE_BYTES:
                # 轮询粒度间隙内超限：按截断处理，不把残缺窗口当完整上传。
                return "profile_too_large"
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            projected = project_folded_stacks(
                raw_text,
                strip_prefixes=self._strip_prefixes,
            )
        except Exception:
            return "projection_failed"
        if not projected.strip():
            return "empty_profile"
        try:
            self._upload(projected.encode("utf-8"))
        except Exception as exc:
            self._diagnostic = str(exc)
            return "upload_failed"
        return ""

    def _upload(self, content: bytes) -> None:
        endpoint = self.pyroscope_endpoint.rstrip("/") + "/ingest"
        window_start = int(self._epoch_clock())
        params = {
            "name": self.service_name,
            "format": INGEST_FORMAT,
            "units": INGEST_UNITS,
            "aggregationType": INGEST_AGGREGATION_TYPE,
            "sampleRate": INGEST_SAMPLE_RATE,
            "from": window_start,
            "until": window_start + WINDOW_SECONDS,
        }
        uploader = self._uploader
        if uploader is None:
            uploader = _HttpxUploader(self.pyroscope_endpoint)
            self._uploader = uploader
        status = uploader.upload(
            endpoint=endpoint,
            params=params,
            content=content,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"pyroscope ingest returned HTTP {int(status)}")

    # -- 状态迁移 -------------------------------------------------------------

    def _set_error(self, code: str) -> None:
        self._current_error = code
        self._log_failure(code)
        if self._on_status is not None:
            try:
                self._on_status("degraded", code)
            except Exception:
                pass

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._windows_completed += 1
            self._last_success = _utc_now_iso()
            self._current_error = None
        if self._on_status is not None:
            try:
                self._on_status("healthy", None)
            except Exception:
                pass

    def _record_failure(self, code: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._total_failures += 1
            self._last_failure = _utc_now_iso()
            self._current_error = code
        self._log_failure(code)
        if self._on_status is not None:
            try:
                self._on_status("degraded", code)
            except Exception:
                pass

    def _disable(self) -> None:
        with self._lock:
            self._disabled = True
            self._current_error = "sampler_disabled"
        if self._on_status is not None:
            try:
                self._on_status("degraded", "sampler_disabled")
            except Exception:
                pass

    def _log_failure(self, code):
        try:
            # 仅稳定错误码；不输出原始诊断（upload 异常文本可能内嵌
            # pyroscope URL、py-spy stderr 含内部路径）；不写 stdout。
            logging.getLogger(__name__).warning(
                "profile sampling failed code=%s",
                code,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 每 PID 采样器注册表（多 worker：每个 Python PID 一个采样器）
# ---------------------------------------------------------------------------

_SAMPLERS: Dict[int, ProfilingSampler] = {}
_SAMPLERS_LOCK = threading.Lock()

# fork 不变量：注册表按 os.getpid() 键控，fork 出的子进程（不同 PID）
# 绝不继承或复用父进程的采样器。


def get_or_create_profiler(
    settings: Settings, *, instance_id: Optional[str] = None, **overrides: Any
) -> Optional[ProfilingSampler]:
    """返回本 PID 的采样器，首次调用时创建。

    每个 worker 进程（fork 后的每个 Python PID）持有独立采样器，以
    ``instance_id`` 区分；组合根应传入观测句柄的 ``instance_id`` 使
    Profile 与 Trace 资源身份同源。同一 PID 内最多存在一个采样器。
    profiling 未启用时返回 None。"""
    pid = os.getpid()
    with _SAMPLERS_LOCK:
        existing = _SAMPLERS.get(pid)
        if existing is not None:
            return existing
        if not settings.profiling_enabled:
            return None
        sampler = ProfilingSampler(
            instance_id=instance_id or uuid.uuid4().hex,
            pyroscope_endpoint=settings.profiling_pyroscope_endpoint,
            interval_seconds=settings.profiling_interval_seconds,
            **overrides,
        )
        _SAMPLERS[pid] = sampler
        return sampler


def _reset_for_tests() -> None:
    with _SAMPLERS_LOCK:
        _SAMPLERS.clear()
