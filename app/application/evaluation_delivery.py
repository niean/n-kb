# app/application/evaluation_delivery.py
"""评估投递 worker 与 producer-state 心跳控制槽。

职责边界：异步循环经 owner/lease 原子领取到期 outbox 行（claim 在存储事务内
完成，网络投递在租约事务外执行），按接收端分型表回写结果——严格确认删行、
未知确认同字节重放、瞬态失败指数退避（sample 第 20 次实际发送失败进入
dead_letter，控制消息/tombstone 永不放弃）、确定性拒绝按消息类型隔离或留存
待修复、认证失败暂停本 endpoint 全部发送（pending 保留、不耗尽逐行 attempts，
仅可通过修复启动期配置并重启恢复，本模块不提供运行期 token 修改或热重载入口）。

心跳使用独立控制槽：启动后立即发送，之后每 heartbeat_seconds 构造新快照（读取
五计数与递增 sequence 由 counters 端口在同一事务内完成），使用独立 asyncio 定时
不受 sample 循环阻塞或 outbox 满箱饥饿；同一快照重试保留 sequence/observed_at/
正文，新周期替换旧待发快照并生成更高 sequence；409 stale_sequence 丢弃过时心跳
并重建更高 sequence 的新快照，不作为事件冲突。心跳失败不影响样本投递。

退避公式：第 k 次失败基础 min(300, 2*2^(k-1)) 秒，加入不超过基础值 10% 的抖动
且最终不超过 300s；有效 Retry-After 为等待下限（可超过 300s）。

依赖契约（duck type，本模块不 import infrastructure，保持 application 单向依赖）：
- delivery_client.send(message_bytes) -> outcome，outcome 具有 kind
  （accepted|rejected|retry|auth_failed）、code（受控错误码）、retry_after
  （float|None）、body（dict|None）属性；网络与超时由客户端内部归入 retry 分型。
  组合根拥有 client 生命周期：关闭时由组合根调用 client 的 aclose()，worker 不调用。
- outbox_store 提供 claim_due_outbox / record_delivery_attempt（domain 端口契约）。
- counters 提供 snapshot_heartbeat(observed_at) -> bytes（canonical 心跳字节）。
- sleep 兼容 asyncio.sleep 签名；测试注入 fake sleeper 与 fake clock。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone

from app.domain.evaluation import (
    SAMPLE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    canonical_json_bytes,
)

logger = logging.getLogger(__name__)

# sample 第 20 次实际发送失败进入 dead_letter；控制消息（tombstone）不设放弃上限
SAMPLE_GIVE_UP_ATTEMPTS = 20
BACKOFF_CAP_SECONDS = 300.0
# 单次投递最坏耗时口径：投递客户端超时惯用为分阶段 10s（connect/read/write
# 三阶段），租约预算按该口径预留，防止尾行在 ACK 写入前租约过期被静默拒绝
WORST_SEND_SECONDS = 30.0
# 确定性拒绝中保留待修复的分型（重定向属配置错误，消息保留不隔离）
RETAIN_REJECT_CODES = frozenset({"redirect_not_followed"})
# 心跳 409 stale_sequence：丢弃过时快照并重建，不作为事件冲突
STALE_SEQUENCE_CODE = "stale_sequence"

# 分型处置：confirm 删行 / retry 退避重试 / dead_letter 隔离 / retain 留存待修复
DISPOSITION_CONFIRM = "confirm"
DISPOSITION_RETRY = "retry"
DISPOSITION_DEAD_LETTER = "dead_letter"
DISPOSITION_RETAIN = "retain"
DISPOSITION_PAUSE = "pause"


class EvaluationDeliveryWorker:
    """lifespan 启动的异步投递 worker：sample 投递循环 + 心跳控制槽。"""

    def __init__(
        self,
        delivery_client,
        outbox_store,
        counters,
        interval_seconds: float,
        *,
        owner: str,
        lease_seconds: int = 45,
        heartbeat_seconds: float = 60,
        clock=time.time,
        sleep=None,
    ) -> None:
        self._client = delivery_client
        self._store = outbox_store
        self._counters = counters
        self._interval_seconds = interval_seconds
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._stop_event = asyncio.Event()
        self._auth_paused = False
        self._in_flight = 0
        self._heartbeat_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None

    # -- 生命周期 ----------------------------------------------------------

    @property
    def auth_paused(self) -> bool:
        """认证失败暂停状态；仅可通过修复启动期配置并重启恢复。"""
        return self._auth_paused

    async def run(self) -> None:
        """启动投递循环与心跳控制槽；随取消或 close 停止，未确认行保留。"""
        self._run_task = asyncio.current_task()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._sample_loop()
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task

    async def close(self, wait_seconds: float = 15) -> None:
        """停止领取，最多等待 wait_seconds 让在途请求完成，超时后取消任务；
        取消不删除未确认行，重启后继续处理。"""
        self._stop_event.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while self._in_flight > 0 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        tasks = [task for task in (self._heartbeat_task, self._run_task) if task is not None]
        for task in tasks:
            if not task.done() and task is not asyncio.current_task():
                task.cancel()
        for task in tasks:
            if task is asyncio.current_task():
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- sample 投递循环 ----------------------------------------------------

    async def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._auth_paused:
                await self._sleep(self._interval_seconds)
                continue
            claimed = self._claim_due()
            if not claimed:
                await self._sleep(self._interval_seconds)
                continue
            # 批内共享同一 lease_until：尾行有效预算随发送耗时递减；剩余预算
            # 不足最坏发送时长即停止本批，避免 ACK 写入前租约过期被静默拒绝
            batch_deadline = self._clock() + self._lease_seconds - WORST_SEND_SECONDS
            for row in claimed:
                if self._stop_event.is_set() or self._auth_paused:
                    break
                if self._clock() >= batch_deadline:
                    logger.warning(
                        "evaluation delivery: lease budget exhausted, deferring %d claimed rows",
                        len(claimed) - claimed.index(row),
                    )
                    break
                await self._deliver_one(row)

    def _claim_due(self) -> list[dict]:
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        try:
            return self._store.claim_due_outbox(self._owner, self._lease_seconds, now)
        except sqlite3.OperationalError as exc:
            # 仅 SQLite 锁竞争（busy/locked）按可重试处理；其他 OperationalError
            # （如 schema 缺失）属持久化故障，快速失败抛出，避免无限告警循环
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                logger.warning("evaluation delivery: outbox claim busy, retry next interval")
                return []
            raise

    async def _deliver_one(self, row: dict) -> None:
        body = canonical_json_bytes(row["payload"])
        self._in_flight += 1
        try:
            outcome = await self._client.send(body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 客户端契约外的意外异常按网络故障退避重试；日志只含类型名，不含正文
            logger.warning(
                "evaluation delivery: unexpected client failure type=%s", type(exc).__name__
            )
            outcome = None
        finally:
            self._in_flight -= 1
        self._apply_outcome(row, outcome)

    # -- 分型表（spec Component 4） ------------------------------------------

    def _classify(self, outcome, message_type: str) -> str:
        """按分型表映射处置：confirm/retry/dead_letter/retain/pause。

        严格确认补充校验：sample 须有非空 sample_id、tombstone 须有 tombstone==true，
        否则视为未知确认按 retry 处理（重试同字节）。控制消息不设放弃上限，任何
        确定性拒绝都留存待修复并告警。
        """
        if outcome is None:
            return DISPOSITION_RETRY
        kind = outcome.kind
        if kind == "accepted":
            body = outcome.body if isinstance(outcome.body, dict) else {}
            if message_type == SAMPLE_MESSAGE_TYPE and not body.get("sample_id"):
                return DISPOSITION_RETRY
            if message_type == TARGET_DELETED_MESSAGE_TYPE and body.get("tombstone") is not True:
                return DISPOSITION_RETRY
            return DISPOSITION_CONFIRM
        if kind == "auth_failed":
            return DISPOSITION_PAUSE
        if kind == "retry":
            return DISPOSITION_RETRY
        # kind == rejected：确定性拒绝
        code = self._outcome_code(outcome, "rejected")
        if code in RETAIN_REJECT_CODES:
            return DISPOSITION_RETAIN
        if message_type == TARGET_DELETED_MESSAGE_TYPE:
            return DISPOSITION_RETAIN
        return DISPOSITION_DEAD_LETTER

    def _apply_outcome(self, row: dict, outcome) -> None:
        disposition = self._classify(outcome, row["message_type"])
        if disposition == DISPOSITION_CONFIRM:
            self._confirm(row)
            return
        if disposition == DISPOSITION_PAUSE:
            self._pause()
            return
        if disposition == DISPOSITION_DEAD_LETTER:
            self._record_failure(
                row, self._outcome_code(outcome, "rejected"), dead_letter=True
            )
            return
        if disposition == DISPOSITION_RETAIN:
            self._record_failure(row, self._outcome_code(outcome, "rejected"))
            # 留存待修复属于需要人工介入的确定性拒绝：统一告警（只含类型与受控 code）
            logger.warning(
                "evaluation delivery: message retained type=%s code=%s",
                row["message_type"],
                self._outcome_code(outcome, "rejected"),
            )
            return
        self._record_failure(
            row,
            self._outcome_code(outcome, "network_error"),
            retry_after=getattr(outcome, "retry_after", None),
        )

    # -- 存储回写 ------------------------------------------------------------

    def _confirm(self, row: dict) -> None:
        if row["message_type"] == TARGET_DELETED_MESSAGE_TYPE:
            # 控制消息严格 ACK：只置 remote_acked，行保留——远端 ACK 与本地清理
            # 状态分别持久化，二者都完成才由外部清理（T8 恢复入口）删行
            try:
                self._store.mark_remote_acked(
                    row["producer_event_id"],
                    owner=self._owner,
                    now=datetime.fromtimestamp(self._clock(), tz=timezone.utc),
                )
            except sqlite3.OperationalError:
                logger.warning(
                    "evaluation delivery: remote ack record busy event=%s",
                    row["producer_event_id"],
                )
            return
        try:
            self._store.record_delivery_attempt(
                row["producer_event_id"],
                delete=True,
                status=None,
                attempts=row["attempts"],
                next_retry_at=None,
                last_error=None,
                owner=self._owner,
            )
        except sqlite3.OperationalError:
            logger.warning(
                "evaluation delivery: ack record busy event=%s", row["producer_event_id"]
            )

    def _record_failure(
        self, row: dict, code: str, *, retry_after: float | None = None, dead_letter: bool = False
    ) -> None:
        attempts = row["attempts"] + 1
        if (
            not dead_letter
            and row["message_type"] == SAMPLE_MESSAGE_TYPE
            and attempts >= SAMPLE_GIVE_UP_ATTEMPTS
        ):
            dead_letter = True
        next_retry_at = None
        if not dead_letter:
            delay = self._backoff(attempts, retry_after)
            next_retry_at = self._iso(self._clock() + delay)
        try:
            self._store.record_delivery_attempt(
                row["producer_event_id"],
                delete=False,
                status="dead_letter" if dead_letter else "pending",
                attempts=attempts,
                next_retry_at=next_retry_at,
                last_error=code,
                owner=self._owner,
            )
        except sqlite3.OperationalError:
            logger.warning(
                "evaluation delivery: failure record busy event=%s", row["producer_event_id"]
            )
            return
        if dead_letter:
            logger.warning(
                "evaluation delivery: message quarantined type=%s code=%s attempts=%s",
                row["message_type"],
                code,
                attempts,
            )

    def _pause(self) -> None:
        if not self._auth_paused:
            logger.warning(
                "evaluation delivery: authentication failed (code withheld), "
                "sending paused until config fix and restart"
            )
        self._auth_paused = True

    # -- 心跳控制槽 ----------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        pending: bytes | None = None
        failures = 0
        retry_at: float | None = None
        next_period = self._clock()  # 启动后立即发送
        while not self._stop_event.is_set():
            if self._auth_paused:
                await self._sleep(self._interval_seconds)
                continue
            try:
                now = self._clock()
                if now >= next_period:
                    # 新心跳周期：重建快照（读取五计数+递增 sequence 同事务），
                    # 替换旧待发快照，重试计数归零
                    snapshot = self._build_snapshot()
                    if snapshot is None:
                        await self._sleep(self._interval_seconds)
                        continue
                    pending = snapshot
                    failures = 0
                    retry_at = None
                    next_period = now + self._heartbeat_seconds
                elif pending is None or retry_at is None or now < retry_at:
                    wait = next_period - now
                    if pending is not None and retry_at is not None:
                        wait = min(wait, retry_at - now)
                    await self._sleep(max(wait, 0.001))
                    continue
                # 到期发送：新周期快照或同快照重试
                outcome = await self._send_state(pending)
                if outcome is not None and outcome.kind == "accepted":
                    pending = None
                    continue
                if outcome is not None and outcome.kind == "auth_failed":
                    self._pause()
                    continue
                code = "network_error" if outcome is None else self._outcome_code(outcome, "unknown")
                if outcome is not None and outcome.kind == "rejected":
                    if code == STALE_SEQUENCE_CODE:
                        logger.warning("evaluation delivery: heartbeat stale sequence, snapshot discarded")
                    else:
                        logger.warning("evaluation delivery: heartbeat rejected code=%s", code)
                    # 丢弃快照，一个轮询周期后重建更高 sequence 的新快照
                    pending = None
                    next_period = self._clock() + self._interval_seconds
                    continue
                failures += 1
                retry_after = None if outcome is None else getattr(outcome, "retry_after", None)
                retry_at = self._clock() + self._backoff(failures, retry_after)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 存储或处理故障不得杀死心跳任务（否则 producer-state 静默停报）：
                # 记日志并按退避重试，与 _deliver_one/_send_state 的异常隔离口径一致
                logger.warning(
                    "evaluation delivery: heartbeat iteration failure type=%s", type(exc).__name__
                )
                failures += 1
                retry_at = self._clock() + self._backoff(failures, None)
                # 重建阶段失败时 next_period 仍停留在过去：前推等待目标，使下次
                # 迭代走等待路径（含 await 与退避），避免零睡眠热循环
                if next_period <= self._clock():
                    next_period = self._clock() + self._interval_seconds

    def _build_snapshot(self) -> bytes | None:
        observed_at = datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat()
        try:
            return self._counters.snapshot_heartbeat(observed_at)
        except sqlite3.OperationalError:
            logger.warning("evaluation delivery: heartbeat snapshot busy")
            return None

    async def _send_state(self, body: bytes):
        self._in_flight += 1
        try:
            return await self._client.send(body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "evaluation delivery: unexpected client failure type=%s", type(exc).__name__
            )
            return None
        finally:
            self._in_flight -= 1

    # -- 工具 ----------------------------------------------------------------

    def _backoff(self, attempts: int, retry_after: float | None) -> float:
        """指数退避：base=min(300, 2*2^(k-1))，抖动 <= base*10%，最终 <= 300s；
        有效 Retry-After 作为等待下限（可超过 300s）。

        指数限幅在 min(attempts-1, 30)：attempt 极大时 2**n 转浮点会溢出
        （OverflowError），而结果本就被 300s 上限 cap，限幅不改变取值。"""
        base = min(BACKOFF_CAP_SECONDS, 2.0 * (2 ** min(attempts - 1, 30)))
        delay = min(BACKOFF_CAP_SECONDS, base + random.uniform(0.0, base * 0.1))
        if retry_after is not None and retry_after > delay:
            delay = retry_after
        return delay

    @staticmethod
    def _outcome_code(outcome, default: str) -> str:
        code = getattr(outcome, "code", None)
        return code if isinstance(code, str) and code else default

    @staticmethod
    def _iso(epoch_seconds: float) -> str:
        """next_retry_at 统一 UTC 微秒精度 ISO（与存储侧字典序比较口径一致）。"""
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(
            timespec="microseconds"
        )
