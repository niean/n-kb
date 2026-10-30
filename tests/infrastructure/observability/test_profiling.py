"""Profile 工作器测试（T6 of plan-261029-otel-observability）。

覆盖（spec Components: Profile 工作器；plan T6 四条）：
- 串行窗口循环：窗口不重叠、不做积压补采、不创建无限线程；窗口固定
  10 秒 / 100 Hz，成功后按 interval 等待（可被 shutdown 中断）
- 失败退避 10/30 秒，连续第三次失败停用并报告 degraded；成功清零计数
- watchdog 在窗口 + 5 秒触发（假时钟验证，不等待真实 15 秒）
- 私有临时文件：目录 0700、文件 0600、系统临时目录（绝不在 workspace）；
  16 MiB 上限在采样过程中强制执行（子进程被终止、标 incomplete、不记录
  到结束）
- folded 栈安全投影：动态代码文件名、线程名、异常采样段丢弃；
  workspace/用户目录前缀剥离；全部为空投影报告 empty_profile，绝不上传
  原始文件；上传内容无原始路径/用户目录（无秘密哨兵）
- shutdown：等待可中断，terminate -> kill/wait 2 秒内回收
- 每 PID 一个独立采样器（多 worker 隔离）
- 固定 py-spy 命令行（--pid <self> --format raw --rate 100 --duration 10）
  且绝不传 --locals
- 上传合同：5 秒超时、HTTP 非成功 / attach 失败 / 非零退出均计失败；
  成败都清理临时文件；最多一个窗口在途；stderr 有界且安全投影；
  spike 核验摄取参数（name/format/units/aggregationType/sampleRate/
  from/until），窗口长度 == 10 秒；身份与 Trace 资源同源（service.name）
- 缺少 py-spy：degraded Profile 信号，绝不是应用失败

全部使用假进程/时钟/上传接口，不产生真实 py-spy 流量，不要求 CI 提权。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from app.config import Settings
from app.infrastructure.observability import profiling
from app.infrastructure.observability.profiling import (
    SERVICE_NAME,
    UPLOAD_TIMEOUT_SECONDS,
    WINDOW_SECONDS,
    ProfilingSampler,
    get_or_create_profiler,
    project_folded_stacks,
)


SAFE_FOLDED = (
    "app.main;handle_request (app/main.py:100) 5\n"
    "app.main;handle_request;run_tools (app/main.py:104) 3\n"
)


# ---------------------------------------------------------------------------
# 假件：子进程工厂 / 进程 / 上传接口
# ---------------------------------------------------------------------------


class FakeProcess:
    """行为 callable 驱动的鸭子类型 subprocess.Popen。

    行为在 daemon 线程运行，写 stdout/stderr 文件后必须调用
    ``proc.finish(rc)``。挂死行为只有 ``kill()`` 能结束（单独
    ``terminate()`` 不结束它）。"""

    def __init__(self, *, stdout, stderr, behavior, recorder):
        self.pid = recorder.next_pid()
        self._stdout = stdout
        self._stderr = stderr
        self._behavior = behavior
        self._recorder = recorder
        self._rc: int | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._killed = False
        self._terminated = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        behavior = self._behavior
        if behavior is not None:
            behavior(self)
        if self._rc is None:
            # 挂死行为：存活到 kill()（terminate 不结束）。
            self._done.wait()

    def finish(self, rc: int = 0) -> None:
        with self._lock:
            self._rc = rc
        self._done.set()

    def write_stdout(self, data: bytes) -> None:
        self._stdout.write(data)
        self._stdout.flush()

    def write_stderr(self, data: bytes) -> None:
        self._stderr.write(data)
        self._stderr.flush()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self._rc

    @property
    def returncode(self):
        return self._rc

    def terminate(self) -> None:
        self._terminated = True
        self._recorder.record_terminate(self)

    def kill(self) -> None:
        self._killed = True
        self.finish(-9)

    @property
    def killed(self) -> bool:
        return self._killed


class FakeFactory:
    """记录确切命令行与 spawn 时观察到的临时文件权限；按顺序弹出
    每窗口行为。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pid = 4242
        self.commands: list[list[str]] = []
        self.spawn_records: list[dict] = []
        self.behaviors: deque = deque()
        self.active = 0
        self.max_active = 0
        self.processes: list[FakeProcess] = []

    def queue(self, *behaviors) -> None:
        self.behaviors.extend(behaviors)

    def next_pid(self) -> int:
        with self._lock:
            self._pid += 1
            return self._pid

    def record_terminate(self, proc: FakeProcess) -> None:
        pass

    def __call__(self, command, *, stdout, stderr):
        stdout.flush()
        # 输出路径即 --output 参数（os.fdopen 文件对象的 .name 是 int fd，
        # 不是路径）。
        stdout_path = command[command.index("--output") + 1]
        record = {
            "command": list(command),
            "stdout_path": stdout_path,
            "stdout_mode": os.stat(stdout_path).st_mode & 0o777,
            "parent_dir": os.path.dirname(os.path.abspath(stdout_path)),
            "parent_mode": os.stat(os.path.dirname(os.path.abspath(stdout_path))).st_mode
            & 0o777,
        }
        behavior = self.behaviors.popleft() if self.behaviors else None
        with self._lock:
            self.commands.append(record["command"])
            self.spawn_records.append(record)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        proc = FakeProcess(
            stdout=stdout, stderr=stderr, behavior=behavior, recorder=self
        )

        def _track_exit() -> None:
            proc._done.wait()
            with self._lock:
                self.active -= 1

        threading.Thread(target=_track_exit, daemon=True).start()
        self.processes.append(proc)
        return proc


class FakeUploader:
    """鸭子类型上传客户端；结果为状态码、异常，缺省 200。"""

    def __init__(self, *results):
        self.calls: list[dict] = []
        self.results = deque(results)

    def upload(self, *, endpoint, params, content, timeout):
        self.calls.append(
            {
                "endpoint": endpoint,
                "params": dict(params),
                "content": bytes(content),
                "timeout": timeout,
            }
        )
        result = self.results.popleft() if self.results else 200
        if isinstance(result, Exception):
            raise result
        return result


def make_settings(**overrides) -> Settings:
    defaults = dict(
        profiling_enabled=True,
        profiling_pyroscope_endpoint="http://pyroscope:4040",
        profiling_interval_seconds=10,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def make_sampler(factory, uploader, *, interval=1, **kwargs):
    defaults = dict(
        instance_id="inst-test-1",
        pyroscope_endpoint="http://pyroscope:4040",
        interval_seconds=interval,
        pyspy_binary="py-spy",
        subprocess_factory=factory,
        uploader=uploader,
        poll_interval=0.01,
    )
    defaults.update(kwargs)
    return ProfilingSampler(**defaults)


def wait_until(predicate, timeout=5.0, interval=0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# 固定命令行 / 目标 PID
# ---------------------------------------------------------------------------


class TestFixedCommandLine:
    def test_fixed_command_line_and_no_locals(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        sampler.start()
        assert wait_until(lambda: len(factory.commands) >= 1)
        sampler.stop()
        command = factory.commands[0]
        pid = os.getpid()
        assert command == [
            "py-spy",
            "record",
            "--pid",
            str(pid),
            "--format",
            "raw",
            "--rate",
            "100",
            "--duration",
            "10",
            "--output",
            factory.spawn_records[0]["stdout_path"],
        ]
        assert "--locals" not in command

    def test_window_is_fixed_ten_seconds_regardless_of_interval(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=300)
        sampler.start()
        assert wait_until(lambda: bool(factory.commands))
        sampler.stop()
        assert factory.commands[0][factory.commands[0].index("--duration") + 1] == "10"

    def test_targets_only_current_python_pid(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        sampler.start()
        assert wait_until(lambda: bool(factory.commands))
        sampler.stop()
        assert factory.commands[0][3] == str(os.getpid())


# ---------------------------------------------------------------------------
# 串行窗口 / 无积压
# ---------------------------------------------------------------------------


class TestSerialWindows:
    def test_no_parallel_windows_and_no_backlog(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        started = threading.Event()

        def slow_window(proc: FakeProcess) -> None:
            started.set()
            time.sleep(0.3)
            proc.write_stdout(SAFE_FOLDED.encode())
            proc.finish(0)

        factory.queue(slow_window)
        sampler.start()
        assert started.wait(timeout=5)
        # 上一窗口完成前不得开始下一窗口：并发窗口恰好一个（无并行），
        # 也不在首个窗口运行期间堆积补采窗口。
        assert len(factory.commands) == 1
        sampler.stop()
        assert factory.max_active == 1
        # 窗口严格逐个尝试（串行循环）；新尝试只在上个窗口结束后发生。
        assert sampler.windows_attempted == len(factory.commands)

    def test_success_waits_interval_before_next_window(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sleeps: list[float] = []
        sampler = make_sampler(factory, uploader, interval=7, sleeper=sleeps.append)
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )
        sampler.start()
        assert wait_until(lambda: sampler.windows_completed >= 1, timeout=10)
        time.sleep(0.2)
        sampler.stop()
        assert sampler.windows_completed == 1
        assert sleeps == [7.0]


# ---------------------------------------------------------------------------
# 失败退避 / 停用 / 成功恢复
# ---------------------------------------------------------------------------


class TestFailurePolicy:
    def test_backoff_10_30_then_third_failure_disables(self):
        factory, uploader = FakeFactory(), FakeUploader(500, 500)
        sleeps: list[float] = []
        sampler = make_sampler(
            factory,
            uploader,
            sleeper=sleeps.append,
        )
        # 窗口 1、2 产出合法 profile 但摄取返回 500 -> 失败；窗口 3 非零
        # 退出 -> 失败。
        produced = lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        failed_exit = lambda p: (p.write_stderr(b"error: boom\n"), p.finish(1))
        factory.queue(produced, produced, failed_exit)
        sampler.start()
        assert wait_until(
            lambda: sampler.disabled and len(factory.commands) >= 3, timeout=10
        )
        time.sleep(0.2)
        sampler.stop()
        assert sampler.disabled
        assert sampler.status == "degraded"
        assert sampler.current_error == "sampler_disabled"
        assert sleeps == [10.0, 30.0]
        # 第三次失败停用了采样器：不再有后续窗口。
        assert len(factory.commands) == 3

    def test_success_resets_failure_counter(self):
        factory, uploader = FakeFactory(), FakeUploader(500, 500)
        sleeps: list[float] = []
        statuses: list[tuple] = []
        sampler = make_sampler(
            factory,
            uploader,
            sleeper=sleeps.append,
            on_status=lambda status, code: statuses.append((status, code)),
        )
        # 失败、失败、成功（清零）-> healthy；第三次上传命中缺省 200。
        produced = lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        factory.queue(produced, produced, produced)
        sampler.start()
        assert wait_until(
            lambda: sampler.windows_completed >= 1 and sampler.consecutive_failures == 0,
            timeout=10,
        )
        sampler.stop()
        assert sampler.windows_completed == 1
        assert sampler.consecutive_failures == 0
        assert sampler.status == "healthy"
        assert not sampler.disabled
        assert ("healthy", None) in statuses

    def test_disable_reports_degraded_via_on_status(self):
        factory, uploader = FakeFactory(), FakeUploader(500, 500)
        statuses: list[tuple] = []
        sampler = make_sampler(
            factory,
            uploader,
            sleeper=lambda s: None,
            on_status=lambda status, code: statuses.append((status, code)),
        )
        produced = lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        failed_exit = lambda p: (p.write_stderr(b"error: boom\n"), p.finish(1))
        factory.queue(produced, produced, failed_exit)
        sampler.start()
        assert wait_until(lambda: sampler.disabled, timeout=10)
        sampler.stop()
        assert ("degraded", "sampler_disabled") in statuses
        # 成功路径的 healthy 上报在下方成功恢复测试中覆盖。
        assert all(status != "healthy" for status, _ in statuses)


# ---------------------------------------------------------------------------
# watchdog（假时钟）
# ---------------------------------------------------------------------------


class TestWatchdog:
    def test_watchdog_fires_at_window_plus_5s(self):
        factory, uploader = FakeFactory(), FakeUploader()
        # 时钟值：起点 0.0 -> 窗口+5s 截止 15.0；14.9 未到，15.1 触发。
        values = deque([0.0, 14.9, 15.1, 100.0, 101.0, 102.0, 103.0])
        sampler = make_sampler(
            factory,
            uploader,
            interval=1,
            clock=lambda: values.popleft() if values else 104.0,
        )

        def hang(proc: FakeProcess) -> None:
            threading.Event().wait(30)  # 远超 watchdog

        factory.queue(hang)
        sampler.start()
        assert wait_until(
            lambda: sampler.current_error == "watchdog_timeout", timeout=10
        )
        sampler.stop()
        # watchdog = 窗口 10s + 5s slack；假时钟验证 14.9 不触发、15.1 触发。
        assert factory.processes[0]._terminated
        assert sampler.consecutive_failures == 1
        assert sampler.windows_incomplete == 1
        assert uploader.calls == []  # 截断的窗口绝不上传

    def test_watchdog_does_not_fire_before_window_plus_slack(self):
        factory, uploader = FakeFactory(), FakeUploader()
        # 时钟停在窗口内（10s 窗口 + 5s slack 之前）-> watchdog 不触发，
        # 子进程自行退出后正常采集。
        values = deque([0.0, 14.9])
        sampler = make_sampler(
            factory,
            uploader,
            interval=1,
            clock=lambda: values.popleft() if values else 14.95,
        )
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )
        sampler.start()
        assert wait_until(lambda: bool(uploader.calls), timeout=10)
        sampler.stop()
        assert sampler.windows_completed == 1
        assert sampler.current_error is None


# ---------------------------------------------------------------------------
# 临时文件：权限、位置、运行中 16 MiB 上限
# ---------------------------------------------------------------------------


class TestTempFiles:
    def test_private_temp_file_permissions_0700_0600(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, temp_root=None)
        sampler.start()
        assert wait_until(lambda: bool(factory.spawn_records))
        sampler.stop()
        record = factory.spawn_records[0]
        assert record["stdout_mode"] == 0o600
        assert record["parent_mode"] == 0o700

    def test_no_workspace_temp_files_system_temp_only(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        sampler.start()
        assert wait_until(lambda: bool(factory.spawn_records))
        sampler.stop()
        parent = Path(factory.spawn_records[0]["parent_dir"]).resolve()
        system_temp = Path(tempfile.gettempdir()).resolve()
        assert parent == system_temp or system_temp in parent.parents
        repo_root = Path(__file__).resolve().parents[3]
        assert repo_root not in parent.parents and parent != repo_root

    def test_16mib_cap_enforced_during_operation(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)

        killed_during_operation = threading.Event()

        def grower(proc: FakeProcess) -> None:
            chunk = b"x" * (1024 * 1024)
            while not proc._killed:
                proc.write_stdout(chunk)
                time.sleep(0.005)
            killed_during_operation.set()
            proc.finish(-9)

        factory.queue(grower)
        sampler.start()
        started = time.monotonic()
        assert wait_until(
            lambda: killed_during_operation.is_set(), timeout=10
        )
        elapsed = time.monotonic() - started
        sampler.stop()
        # 采样过程中被终止（1MiB/5ms 速率下上限 ~16MiB），远早于 10s 窗口
        # + 5s watchdog 可能触发的时间。
        assert elapsed < 10.0, elapsed
        assert sampler.current_error == "profile_too_large"
        assert sampler.windows_incomplete == 1  # 达限窗口标 incomplete
        assert sampler.windows_completed == 0
        assert factory.processes[0]._terminated
        assert uploader.calls == []  # 绝不上传


# ---------------------------------------------------------------------------
# 栈投影
# ---------------------------------------------------------------------------


class TestStackProjection:
    def test_drops_dynamic_filenames_thread_names_exception_frames(self):
        raw = (
            # 安全帧
            "app.main;handle_request (app/main.py:100) 5\n"
            # 动态代码文件名
            "do_eval (<string>:1) 3\n"
            "bootstrap (<frozen importlib._bootstrap>:219) 1\n"
            # 线程名 / 异常采样段
            "main;worker (app/w.py:9) 4\n"
            "Thread 0x7f88 (active);worker (app/w.py:9) 2\n"
            "[GIL active];worker (app/w.py:9) 1\n"
        )
        projected = project_folded_stacks(raw, strip_prefixes=("/ws",))
        lines = projected.splitlines()
        # "app.main" 是裸段（无 file:line）-> 与线程名一样丢弃；只有带
        # 安全 file:line 的帧存活。
        assert "handle_request (app/main.py:100) 5" in lines
        assert "worker (app/w.py:9) 4" in lines
        assert "worker (app/w.py:9) 2" in lines
        assert "worker (app/w.py:9) 1" in lines
        joined = "\n".join(lines)
        assert "app.main" not in joined
        assert "<string>" not in joined
        assert "<frozen" not in joined
        assert "main;" not in joined
        assert "Thread" not in joined
        assert "GIL" not in joined

    def test_strips_workspace_and_user_prefixes(self):
        raw = "fn (/Users/me/kb/app/main.py:12) 2\n"
        projected = project_folded_stacks(raw, strip_prefixes=("/Users/me/kb",))
        assert "(app/main.py:12) 2" in projected
        assert "/Users/me" not in projected

    def test_all_empty_projection_reports_empty_profile_no_upload(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)

        def only_unsafe(proc: FakeProcess) -> None:
            proc.write_stdout(b"do_eval (<string>:1) 9\nmain;f (<string>:2) 3\n")
            proc.finish(0)

        factory.queue(only_unsafe)
        sampler.start()
        assert wait_until(lambda: sampler.windows_attempted >= 1, timeout=10)
        time.sleep(0.2)
        sampler.stop()
        assert sampler.current_error == "empty_profile"
        assert uploader.calls == []

    def test_upload_content_is_projected_bounded_text(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        factory.queue(
            lambda p: (
                p.write_stdout(
                    b"app.main;h (app/main.py:1) 2\ndo_eval (<string>:1) 1\n"
                ),
                p.finish(0),
            )
        )
        sampler.start()
        assert wait_until(lambda: bool(uploader.calls), timeout=10)
        sampler.stop()
        content = uploader.calls[0]["content"].decode()
        assert "h (app/main.py:1) 2" in content
        assert "app.main" not in content
        assert "<string>" not in content

    def test_upload_never_carries_raw_paths_or_user_home(self):
        factory, uploader = FakeFactory(), FakeUploader()
        # 缺省 strip_prefixes = (os.getcwd(), 用户目录)：原始绝对路径与
        # 用户目录（秘密哨兵）绝不出现在上传载荷中。
        raw = (
            f"fn ({os.getcwd()}/app/main.py:1) 2\n"
            f"fn2 ({Path.home()}/elsewhere/mod.py:7) 1\n"
        ).encode()
        sampler = make_sampler(factory, uploader)
        factory.queue(lambda p: (p.write_stdout(raw), p.finish(0)))
        sampler.start()
        assert wait_until(lambda: bool(uploader.calls), timeout=10)
        sampler.stop()
        content = uploader.calls[0]["content"].decode()
        assert os.getcwd() not in content
        assert str(Path.home()) not in content
        assert "app/main.py:1" in content


# ---------------------------------------------------------------------------
# shutdown：2 秒内 terminate -> kill/wait
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_terminate_then_kill_within_2s(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        factory.queue(lambda p: time.sleep(60))  # 挂死
        sampler.start()
        assert wait_until(lambda: bool(factory.processes))
        proc = factory.processes[0]
        started = time.monotonic()
        sampler.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, elapsed
        assert proc._terminated
        assert proc.killed
        assert not sampler.is_running()

    def test_stop_interrupts_interval_wait_quickly(self):
        factory, uploader = FakeFactory(), FakeUploader()
        # 缺省 sleeper（可中断等待）：成功窗口后进入 interval 等待，
        # stop 必须立即打断，不等待 interval（这里 60 秒）结束。
        sampler = make_sampler(factory, uploader, interval=60)
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )
        sampler.start()
        assert wait_until(lambda: sampler.windows_completed >= 1, timeout=10)
        started = time.monotonic()
        assert sampler.stop() is True
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, elapsed
        assert not sampler.is_running()

    def test_registers_stop_callback_on_handle(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        registered: list = []

        class FakeHandle:
            def register_stop_callback(self, callback):
                registered.append(callback)
                return True

        assert sampler.register_with_handle(FakeHandle()) is True
        assert registered == [sampler.stop]


# ---------------------------------------------------------------------------
# 多 worker：每 PID 一个独立采样器
# ---------------------------------------------------------------------------


class TestPerPidSamplers:
    def test_same_pid_returns_same_sampler(self):
        profiling._reset_for_tests()
        try:
            settings = make_settings()
            first = get_or_create_profiler(settings, instance_id="i-1")
            second = get_or_create_profiler(settings, instance_id="i-1")
            assert first is second
        finally:
            profiling._reset_for_tests()

    def test_forked_child_pid_gets_independent_sampler(self):
        profiling._reset_for_tests()
        try:
            settings = make_settings()
            parent = get_or_create_profiler(settings, instance_id="i-1")
            original_getpid = os.getpid
            os.getpid = lambda: 987654
            try:
                child = get_or_create_profiler(settings, instance_id="i-2")
            finally:
                os.getpid = original_getpid
            assert child is not parent
            assert child.instance_id == "i-2"
            assert child.target_pid == 987654
        finally:
            profiling._reset_for_tests()

    def test_disabled_profiling_returns_none(self):
        profiling._reset_for_tests()
        try:
            settings = make_settings(profiling_enabled=False)
            assert get_or_create_profiler(settings) is None
        finally:
            profiling._reset_for_tests()

    def test_sampler_reads_settings_and_identity_source(self):
        profiling._reset_for_tests()
        try:
            settings = make_settings(
                profiling_pyroscope_endpoint="http://pyroscope:4040",
                profiling_interval_seconds=120,
            )
            sampler = get_or_create_profiler(settings, instance_id="handle-inst")
            assert sampler is not None
            # 身份与 Trace 资源同源：service.name 来自 setup（T4 Resource
            # 同一常量），instance_id 由组合根传入观测句柄的同一值。
            assert sampler.service_name == SERVICE_NAME == "n-kb"
            assert sampler.instance_id == "handle-inst"
            assert sampler.pyroscope_endpoint == "http://pyroscope:4040"
            assert sampler.interval_seconds == 120
            snapshot = sampler.status_snapshot()
            assert snapshot["service_name"] == "n-kb"
            assert snapshot["instance_id"] == "handle-inst"
        finally:
            profiling._reset_for_tests()


# ---------------------------------------------------------------------------
# 上传合同
# ---------------------------------------------------------------------------


class TestUploadContract:
    def _run_one_window(self, factory, uploader, **kwargs):
        sampler = make_sampler(factory, uploader, **kwargs)
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )
        sampler.start()
        assert wait_until(lambda: bool(uploader.calls), timeout=10)
        sampler.stop()
        return sampler

    def test_upload_5s_timeout_and_spike_verified_params(self):
        factory, uploader = FakeFactory(), FakeUploader()
        self._run_one_window(factory, uploader)
        call = uploader.calls[0]
        assert call["timeout"] == UPLOAD_TIMEOUT_SECONDS == 5.0
        params = call["params"]
        assert params["name"] == "n-kb"
        assert params["format"] == "folded"
        assert params["units"] == "samples"
        assert params["aggregationType"] == "sum"
        assert params["sampleRate"] == 100
        assert params["until"] - params["from"] == WINDOW_SECONDS == 10
        assert call["endpoint"] == "http://pyroscope:4040/ingest"

    def test_http_non_success_counts_as_failure_and_cleans_up(self):
        factory, uploader = FakeFactory(), FakeUploader(503)
        sampler = self._run_one_window(factory, uploader)
        assert sampler.consecutive_failures == 1
        assert sampler.current_error == "upload_failed"
        for record in factory.spawn_records:
            assert not os.path.exists(record["stdout_path"])
            assert not os.path.exists(record["parent_dir"])

    def test_transport_exception_counts_as_failure(self):
        factory, uploader = FakeFactory(), FakeUploader(
            RuntimeError("connection refused")
        )
        sampler = self._run_one_window(factory, uploader)
        assert sampler.consecutive_failures == 1
        assert sampler.current_error == "upload_failed"
        assert not os.path.exists(factory.spawn_records[0]["parent_dir"])

    def test_nonzero_exit_counts_as_failure(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        factory.queue(lambda p: (p.write_stderr(b"error: no space left\n"), p.finish(1)))
        sampler.start()
        assert wait_until(lambda: sampler.windows_attempted >= 1, timeout=10)
        time.sleep(0.2)
        sampler.stop()
        assert sampler.current_error == "subprocess_failed"
        assert uploader.calls == []
        assert not os.path.exists(factory.spawn_records[0]["parent_dir"])

    def test_attach_failure_classified_from_bounded_stderr(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        secret = b"SECRET-STDERR-MARKER failed to attach: Operation not permitted\n"
        factory.queue(lambda p: (p.write_stderr(secret + b"x" * 1024 * 1024), p.finish(1)))
        sampler.start()
        assert wait_until(lambda: sampler.windows_attempted >= 1, timeout=10)
        time.sleep(0.2)
        sampler.stop()
        assert sampler.current_error == "attach_failed"
        # stderr 保持有界，绝不原文投影进状态。
        snapshot = sampler.status_snapshot()
        assert "SECRET-STDERR-MARKER" not in str(snapshot)
        assert "x" * 1000 not in str(snapshot)

    def test_success_and_failure_both_clean_up_temp_files(self):
        factory, uploader = FakeFactory(), FakeUploader()
        self._run_one_window(factory, uploader)
        for record in factory.spawn_records:
            assert not os.path.exists(record["stdout_path"])
            assert not os.path.exists(record["parent_dir"])

    def test_at_most_one_window_in_flight(self):
        factory, uploader = FakeFactory(), FakeUploader()
        self._run_one_window(factory, uploader)
        assert factory.max_active == 1


# ---------------------------------------------------------------------------
# 缺少 py-spy -> degraded 信号，绝不是应用失败
# ---------------------------------------------------------------------------


class TestMissingBinary:
    def test_missing_pyspy_reports_degraded_without_spawning(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, pyspy_binary=None)
        assert sampler.start() is False
        assert sampler.status == "degraded"
        assert sampler.current_error == "pyspy_missing"
        assert factory.commands == []
        sampler.stop()

    def test_missing_binary_is_not_an_app_failure(self):
        # start() 必须返回普通 False（degraded 信号），绝不抛异常。
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, pyspy_binary="")
        try:
            result = sampler.start()
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"missing py-spy raised: {exc!r}")
        assert result is False


# ---------------------------------------------------------------------------
# 健壮性：意外失败让信号 degraded，绝不静默杀死 worker、绝不抛入应用
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_factory_raising_reports_attach_failed(self):
        def broken_factory(command, *, stdout, stderr):
            raise OSError("spawn failed")

        sampler = make_sampler(broken_factory, FakeUploader())
        sampler.start()
        assert wait_until(lambda: sampler.current_error == "attach_failed", timeout=10)
        sampler.stop()
        assert sampler.status == "degraded"
        assert not sampler.disabled

    def test_mkdtemp_raising_reports_temp_failed(self, monkeypatch):
        sampler = make_sampler(FakeFactory(), FakeUploader())

        def broken_mkdtemp(*args, **kwargs):
            raise OSError("no usable temp dir")

        monkeypatch.setattr(profiling.tempfile, "mkdtemp", broken_mkdtemp)
        sampler.start()
        assert wait_until(lambda: sampler.current_error == "temp_failed", timeout=10)
        sampler.stop()
        assert sampler.status == "degraded"
        assert not sampler.disabled

    def test_fdopen_failure_reports_temp_failed_and_closes_raw_fd(self, monkeypatch):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        real_open = os.open
        opened: list[int] = []

        def fake_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            opened.append(fd)
            if len(opened) == 2:
                # 第二个文件（stderr）无法打开。
                os.close(fd)
                raise OSError("fd exhausted")
            return fd

        monkeypatch.setattr(profiling.os, "open", fake_open)
        sampler.start()
        assert wait_until(lambda: sampler.current_error == "temp_failed", timeout=10)
        sampler.stop()
        # 未 spawn 子进程，且已打开的 raw fd 已关闭（无 fd 泄漏）。
        assert factory.commands == []
        with pytest.raises(OSError):
            os.fstat(opened[0])

    def test_projection_raising_reports_projection_failed(self, monkeypatch):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )

        def broken_project(*args, **kwargs):
            raise ValueError("projection exploded")

        monkeypatch.setattr(profiling, "project_folded_stacks", broken_project)
        sampler.start()
        assert wait_until(
            lambda: sampler.current_error == "projection_failed", timeout=10
        )
        sampler.stop()
        # 投影失败绝不上传（无原始文件回退）。
        assert uploader.calls == []

    def test_unexpected_window_exception_reports_sampler_error(self, monkeypatch):
        sampler = make_sampler(FakeFactory(), FakeUploader())

        def broken_poll(self):
            raise RuntimeError("poll exploded")

        monkeypatch.setattr(FakeProcess, "poll", broken_poll)
        sampler.start()
        # 顶层守卫让信号 degraded，而不是 worker 线程静默死亡、状态仍 healthy。
        assert wait_until(lambda: sampler.current_error == "sampler_error", timeout=10)
        sampler.stop()
        assert sampler.status == "degraded"
        assert not sampler.disabled

    def test_flaky_clock_does_not_abort_window(self):
        # clock 故障由 _safe_now 的 monotonic 兜底吸收：窗口照常完成并上传，
        # 不再逃逸为 sampler_error（watchdog/回收不得因时钟故障失败）。
        real_clock = time.monotonic
        calls = {"n": 0}

        def flaky_clock():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("clock exploded")
            return real_clock()

        factory, uploader = FakeFactory(), FakeUploader()
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )
        sampler = make_sampler(factory, uploader, clock=flaky_clock)
        sampler.start()
        assert wait_until(lambda: bool(uploader.calls), timeout=10)
        sampler.stop()
        assert sampler.current_error != "sampler_error"

    def test_thread_start_failure_reports_degraded_never_raises(self, monkeypatch):
        sampler = make_sampler(FakeFactory(), FakeUploader())

        def broken_start(inner):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", broken_start)
        assert sampler.start() is False
        assert sampler.current_error == "sampler_error"
        assert sampler.status == "degraded"
        assert not sampler.is_running()
        sampler.stop()

    def test_stop_during_window_no_upload_no_spurious_failure(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)

        def output_then_hang(proc: FakeProcess) -> None:
            proc.write_stdout(SAFE_FOLDED.encode())
            proc._done.wait()  # 忽略 terminate；stop() 会 kill 它

        factory.queue(output_then_hang)
        sampler.start()
        assert wait_until(lambda: bool(factory.commands))
        time.sleep(0.05)
        assert sampler.stop() is True
        # 被 stop 的窗口既不上传，也不误判为失败（子进程收到 SIGTERM 可能
        # 以任意码退出）。
        assert uploader.calls == []
        assert sampler.current_error is None
        assert sampler.consecutive_failures == 0
        assert sampler.windows_incomplete == 0

    def test_start_after_stop_returns_false(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        factory.queue(lambda p: time.sleep(60))
        assert sampler.start() is True
        sampler.stop()
        # stop 后不支持重启；应通过 get_or_create_profiler 新建采样器。
        assert sampler.start() is False
        assert not sampler.is_running()

    def test_register_with_handle_returns_false_when_registration_fails(self):
        class DecliningHandle:
            def register_stop_callback(self, callback):
                return False

        sampler = make_sampler(FakeFactory(), FakeUploader())
        assert sampler.register_with_handle(DecliningHandle()) is False
