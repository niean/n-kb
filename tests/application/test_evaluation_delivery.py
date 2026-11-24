"""EvaluationDeliveryWorker 应用层测试：投递分型、退避、认证暂停、心跳控制槽与租约竞争。

fake clock + fake sleeper + fake client 驱动异步循环（不依赖真实等待）；租约竞争与
重启重放使用真实 SQLiteStore，并 monkeypatch `_utc_now_iso` 对齐 fake clock，避免
真实墙钟参与租约比较。fake store 按 T2 契约模拟 claim/record 的 owner/lease 语义。

心跳与 sample 共享 fake client：统一按消息类型路由 outcome，断言一律按消息类型
过滤请求，避免心跳立即发送干扰 sample 断言。fake sleeper 每次睡眠推进 fake clock
后让出控制权，因此两个循环交错会使时钟相对目标等待多出一个 interval，时间类
断言使用 [目标, 目标+interval] 容差区间。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.application.evaluation_delivery import (
    SAMPLE_GIVE_UP_ATTEMPTS,
    EvaluationDeliveryWorker,
)
from app.domain.evaluation import (
    PRODUCER_STATE_COUNT_KEYS,
    SAMPLE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    build_sample_message,
    build_state_message,
    build_target_deleted_message,
    canonical_json_bytes,
)
from app.infrastructure.evaluation.producer_client import DeliveryOutcome
from app.infrastructure.persistence.sqlite_store import SQLiteStore

SAMPLE_INTEGRITY = {
    "original_bytes": 1,
    "truncated": False,
    "incomplete": False,
    "observed_bytes": 1,
    "redaction_version": "v0",
}
STATE_MESSAGE_TYPE = "evaluation.producer-state.v1"


# ---------------------------------------------------------------------------
# fake 时钟、sleeper、client、store、counters
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def iso_epoch(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="microseconds")


class FakeSleeper:
    """记录每次 sleep 时长并推进 fake clock，随后让出事件循环控制权。"""

    def __init__(self, clock):
        self.clock = clock
        self.delays = []

    async def sleep(self, delay):
        self.delays.append(delay)
        self.clock.advance(delay)
        await asyncio.sleep(0)


class TypeRouter:
    """按消息类型路由 outcome；各类型独立队列，末尾元素粘滞重复。"""

    def __init__(self, outcomes_by_type):
        self.outcomes = {key: list(value) for key, value in outcomes_by_type.items()}

    def __call__(self, body):
        message_type = json.loads(body)["type"]
        queue = self.outcomes[message_type]
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


class FakeDeliveryClient:
    """fake 投递客户端：TypeRouter handler 或 gate 阻塞，记录每次请求的 body 与时钟。"""

    def __init__(self, clock, handler=None, gate=None):
        self.clock = clock
        self.handler = handler
        self.gate = gate
        self.requests = []

    async def send(self, body):
        self.requests.append({"body": bytes(body), "clock": self.clock()})
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)
        return self.handler(bytes(body))


class FakeOutboxStore:
    """按 T2 契约模拟 claim_due_outbox / record_delivery_attempt 的 owner/lease 语义。"""

    def __init__(self, clock):
        self.clock = clock
        self.rows = {}
        self.claim_calls = []
        self.record_calls = []
        self.mark_remote_acked_calls = []
        # 一次性注入的 claim 异常（None 表示正常），用于 OperationalError 分类测试
        self.claim_error = None

    def add_sample(self, event_id, payload=None):
        payload = payload or sample_payload(event_id)
        self._add_row(event_id, payload)

    def add_tombstone(self, control_key, payload=None):
        payload = payload or tombstone_payload()
        self._add_row(control_key, payload)

    def _add_row(self, event_id, payload):
        self.rows[event_id] = {
            "producer_event_id": event_id,
            "message_type": payload["type"],
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "next_retry_at": None,
            "last_error": None,
            "owner": None,
            "lease_until": None,
            "remote_acked": 0,
        }

    def claim_due_outbox(self, owner, lease_seconds, now):
        if self.claim_error is not None:
            error, self.claim_error = self.claim_error, None
            raise error
        now_iso = now.astimezone(timezone.utc).isoformat(timespec="microseconds")
        lease_until = (now + timedelta(seconds=lease_seconds)).astimezone(
            timezone.utc
        ).isoformat(timespec="microseconds")
        self.claim_calls.append({"owner": owner, "now": now_iso})
        claimed = []
        for row in self.rows.values():
            # 与 T2 契约一致：已远端 ACK 的控制行不再重领（等外部清理删行）
            if row["message_type"] == TARGET_DELETED_MESSAGE_TYPE and row["remote_acked"]:
                continue
            lease_expired = row["owner"] is None or row["lease_until"] <= now_iso
            retry_due = row["next_retry_at"] is None or row["next_retry_at"] <= now_iso
            if row["status"] != "pending" or not lease_expired or not retry_due:
                continue
            row["owner"] = owner
            row["lease_until"] = lease_until
            claimed.append(
                {
                    "producer_event_id": row["producer_event_id"],
                    "message_type": row["message_type"],
                    "payload": row["payload"],
                    "attempts": row["attempts"],
                    "owner": owner,
                    "lease_until": lease_until,
                }
            )
        return claimed

    def record_delivery_attempt(
        self, producer_event_id, *, delete, status, attempts, next_retry_at, last_error, owner
    ):
        self.record_calls.append(
            {
                "producer_event_id": producer_event_id,
                "delete": delete,
                "status": status,
                "attempts": attempts,
                "next_retry_at": next_retry_at,
                "last_error": last_error,
                "owner": owner,
                "clock": self.clock(),
            }
        )
        row = self.rows[producer_event_id]
        now_iso = iso_epoch(self.clock())
        if row["owner"] != owner or row["lease_until"] <= now_iso or row["status"] != "pending":
            return
        if delete:
            del self.rows[producer_event_id]
            return
        if status is not None:
            row["status"] = status
        row["attempts"] = attempts
        row["next_retry_at"] = next_retry_at
        row["last_error"] = last_error

    def mark_remote_acked(self, producer_event_id, *, owner, now):
        """按 T2 契约模拟：owner 与租约校验通过后置 remote_acked=1，行保留。"""
        self.mark_remote_acked_calls.append(
            {
                "producer_event_id": producer_event_id,
                "owner": owner,
                "clock": self.clock(),
            }
        )
        row = self.rows[producer_event_id]
        now_iso = now.astimezone(timezone.utc).isoformat(timespec="microseconds")
        if row["owner"] != owner or row["lease_until"] <= now_iso:
            return
        row["remote_acked"] = 1


class FakeCounters:
    """按 T2 契约模拟 snapshot_heartbeat：每次调用递增 sequence 并返回 canonical 字节。"""

    def __init__(self, epoch="2026-01-01T00:00:00+00:00"):
        self.epoch = epoch
        self.sequence = 0
        self.snapshot_calls = []
        self.counts = {key: 0 for key in PRODUCER_STATE_COUNT_KEYS}

    def snapshot_heartbeat(self, observed_at):
        self.snapshot_calls.append(observed_at)
        self.sequence += 1
        return canonical_json_bytes(
            build_state_message(
                counter_epoch=self.epoch,
                sequence=self.sequence,
                counts=dict(self.counts),
                observed_at=observed_at,
            )
        )


class FlakyCounters(FakeCounters):
    """前 fail_times 次 snapshot_heartbeat 抛非 OperationalError 异常，验证心跳循环存活。

    attempts 统计包含失败在内的全部重建尝试，用于确认心跳确实被调度执行。"""

    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times
        self.attempts = 0

    def snapshot_heartbeat(self, observed_at):
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("snapshot storage exploded")
        return super().snapshot_heartbeat(observed_at)


# ---------------------------------------------------------------------------
# 构造辅助与运行驱动
# ---------------------------------------------------------------------------


def sample_payload(event_id="evt-1"):
    return build_sample_message(
        producer_event_id=event_id,
        retrieval_id=f"r-{event_id}",
        captured_at="2026-01-01T00:00:00+00:00",
        query="q",
        top_k=3,
        filters={},
        results=[],
        corpus_revision="rev-1",
        trace_ref={},
        integrity=dict(SAMPLE_INTEGRITY),
    )


def tombstone_payload(document_id="doc-1"):
    return build_target_deleted_message(
        document_id=document_id, document_revision="hash-1", reason="user_request"
    )


ACCEPTED_SAMPLE = DeliveryOutcome(kind="accepted", body={"status": "accepted", "sample_id": "s-1"})
ACCEPTED_TOMBSTONE = DeliveryOutcome(kind="accepted", body={"status": "accepted", "tombstone": True})
ACCEPTED_STATE = DeliveryOutcome(kind="accepted", body={"status": "accepted", "applied": True})
RETRY_500 = DeliveryOutcome(kind="retry", code="remote_server_error")


def make_worker(clock, store, counters, client, *, interval=5.0, heartbeat_seconds=60.0, owner="w1", lease_seconds=45):
    sleeper = FakeSleeper(clock)
    worker = EvaluationDeliveryWorker(
        client,
        store,
        counters,
        interval,
        owner=owner,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        clock=clock,
        sleep=sleeper.sleep,
    )
    return worker, sleeper


async def run_until(worker, predicate, max_cycles=800):
    """以事件循环协作调度驱动 worker.run()，直到 predicate 成立或轮次耗尽。"""
    task = asyncio.create_task(worker.run())
    for _ in range(max_cycles):
        if predicate():
            break
        await asyncio.sleep(0)
    return task


def requests_of_type(client, message_type):
    return [
        entry
        for entry in client.requests
        if json.loads(entry["body"]).get("type") == message_type
    ]


def parse_body(request_entry):
    return json.loads(request_entry["body"])


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(clock):
    return FakeOutboxStore(clock)


@pytest.fixture
def counters():
    return FakeCounters()


# ---------------------------------------------------------------------------
# 严格确认与未知确认
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_acceptance_deletes_sample_row(clock, store, counters):
    store.add_sample("evt-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: "evt-1" not in store.rows)
    await worker.close()

    assert "evt-1" not in store.rows
    assert len(requests_of_type(client, SAMPLE_MESSAGE_TYPE)) == 1


@pytest.mark.asyncio
async def test_accepted_sample_without_sample_id_retries_same_bytes(clock, store, counters):
    store.add_sample("evt-1")
    weak_ack = DeliveryOutcome(kind="accepted", body={"status": "accepted"})
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {SAMPLE_MESSAGE_TYPE: [weak_ack, weak_ack], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["attempts"] >= 2)
    await worker.close()

    sample_requests = requests_of_type(client, SAMPLE_MESSAGE_TYPE)
    expected = canonical_json_bytes(sample_payload("evt-1"))
    assert sample_requests[0]["body"] == expected
    assert sample_requests[1]["body"] == expected
    row = store.rows["evt-1"]
    assert row["status"] == "pending"
    assert row["attempts"] >= 2


@pytest.mark.asyncio
async def test_accepted_tombstone_without_tombstone_true_retries(clock, store, counters):
    store.add_tombstone("deleted:doc-1")
    weak_ack = DeliveryOutcome(kind="accepted", body={"status": "accepted"})
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {TARGET_DELETED_MESSAGE_TYPE: [weak_ack, weak_ack], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["deleted:doc-1"]["attempts"] >= 2)
    await worker.close()

    tombstone_requests = requests_of_type(client, TARGET_DELETED_MESSAGE_TYPE)
    assert len(tombstone_requests) >= 2
    assert tombstone_requests[0]["body"] == tombstone_requests[1]["body"]
    row = store.rows["deleted:doc-1"]
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_unknown_ack_204_retries_and_keeps_row(clock, store, counters):
    store.add_sample("evt-1")
    unknown = DeliveryOutcome(kind="retry", code="unknown_ack")
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {SAMPLE_MESSAGE_TYPE: [unknown, ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: "evt-1" not in store.rows)
    await worker.close()

    sample_requests = requests_of_type(client, SAMPLE_MESSAGE_TYPE)
    assert len(sample_requests) == 2
    assert sample_requests[0]["body"] == sample_requests[1]["body"]


# ---------------------------------------------------------------------------
# 409 分型
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_409_event_conflict_dead_letters_sample(clock, store, counters):
    store.add_sample("evt-1")
    conflict = DeliveryOutcome(kind="rejected", code="event_conflict")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [conflict], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["status"] == "dead_letter")
    await worker.close()

    row = store.rows["evt-1"]
    assert row["status"] == "dead_letter"
    assert row["last_error"] == "event_conflict"
    assert row["attempts"] == 1


@pytest.mark.asyncio
async def test_409_deletion_conflict_terminates_late_sample(clock, store, counters):
    store.add_sample("evt-1")
    conflict = DeliveryOutcome(kind="rejected", code="deletion_conflict")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [conflict], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["status"] == "dead_letter")
    await worker.close()

    row = store.rows["evt-1"]
    assert row["status"] == "dead_letter"
    assert row["last_error"] == "deletion_conflict"


@pytest.mark.asyncio
async def test_409_unknown_code_quarantines_sample_and_alerts(clock, store, counters, caplog):
    caplog.set_level(logging.WARNING)
    store.add_sample("evt-1")
    conflict = DeliveryOutcome(kind="rejected", code="conflict_unknown")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [conflict], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["status"] == "dead_letter")
    await worker.close()

    row = store.rows["evt-1"]
    assert row["status"] == "dead_letter"
    assert any("conflict_unknown" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_409_unknown_code_retains_tombstone_and_alerts(clock, store, counters, caplog):
    caplog.set_level(logging.WARNING)
    store.add_tombstone("deleted:doc-1")
    conflict = DeliveryOutcome(kind="rejected", code="conflict_unknown")
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter({TARGET_DELETED_MESSAGE_TYPE: [conflict], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}),
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["deleted:doc-1"]["attempts"] == 1)
    await worker.close()

    row = store.rows["deleted:doc-1"]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["next_retry_at"] is not None
    assert any("conflict_unknown" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# 认证失败暂停
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"), [(401, "invalid_credentials"), (403, "service_mismatch")]
)
@pytest.mark.asyncio
async def test_auth_failure_pauses_all_sending_and_preserves_pending(
    clock, store, counters, code, expected
):
    store.add_sample("evt-1")
    store.add_tombstone("deleted:doc-1")
    auth = DeliveryOutcome(kind="auth_failed", code=expected)
    client = FakeDeliveryClient(clock, handler=lambda body: auth)
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: worker.auth_paused, max_cycles=100)
    for _ in range(40):
        await asyncio.sleep(0)
    await worker.close()

    assert worker.auth_paused is True
    # 两个控制槽各自最多发出一次请求（心跳立即发送一次），之后全部暂停
    assert len(client.requests) <= 2
    for event_id in ("evt-1", "deleted:doc-1"):
        row = store.rows[event_id]
        assert row["status"] == "pending"
        assert row["attempts"] == 0
        assert row["next_retry_at"] is None


# ---------------------------------------------------------------------------
# 413/422/其他确定性 4xx 与 3xx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["payload_too_large", "protocol_invalid", "rejected_4xx", "conflict_unknown"]
)
@pytest.mark.asyncio
async def test_deterministic_rejection_dead_letters_sample(clock, store, counters, code):
    store.add_sample("evt-1")
    rejected = DeliveryOutcome(kind="rejected", code=code)
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [rejected], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["status"] == "dead_letter")
    await worker.close()

    row = store.rows["evt-1"]
    assert row["status"] == "dead_letter"
    assert row["last_error"] == code


@pytest.mark.asyncio
async def test_size_rejection_retains_tombstone(clock, store, counters):
    store.add_tombstone("deleted:doc-1")
    rejected = DeliveryOutcome(kind="rejected", code="payload_too_large")
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter({TARGET_DELETED_MESSAGE_TYPE: [rejected], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}),
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["deleted:doc-1"]["attempts"] >= 3)
    await worker.close()

    row = store.rows["deleted:doc-1"]
    assert row["status"] == "pending"
    assert row["attempts"] == 3
    assert row["last_error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_redirect_rejection_retains_message_and_alerts(clock, store, counters, caplog):
    caplog.set_level(logging.WARNING)
    store.add_sample("evt-1")
    redirect = DeliveryOutcome(kind="rejected", code="redirect_not_followed")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [redirect], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["attempts"] >= 2)
    await worker.close()

    row = store.rows["evt-1"]
    assert row["status"] == "pending"
    assert row["last_error"] == "redirect_not_followed"
    assert any("redirect_not_followed" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# 指数退避、Retry-After 下限与放弃阈值
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_backoff_progression(clock, store, counters):
    store.add_sample("evt-1")
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {SAMPLE_MESSAGE_TYPE: [RETRY_500, RETRY_500], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["attempts"] >= 2)
    await worker.close()

    first, second = store.record_calls[0], store.record_calls[1]
    delay_first = datetime.fromisoformat(first["next_retry_at"]).timestamp() - first["clock"]
    delay_second = datetime.fromisoformat(second["next_retry_at"]).timestamp() - second["clock"]
    # base = min(300, 2*2^(k-1))，jitter <= base*10%
    assert 2.0 <= delay_first <= 2.2
    assert 4.0 <= delay_second <= 4.4
    assert first["last_error"] == "remote_server_error"


@pytest.mark.asyncio
async def test_retry_after_seconds_raises_floor_above_cap(clock, store, counters):
    store.add_sample("evt-1")
    slow = DeliveryOutcome(kind="retry", code="rate_limited", retry_after=500.0)
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [slow], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["attempts"] >= 1)
    await worker.close()

    record = store.record_calls[0]
    delay = datetime.fromisoformat(record["next_retry_at"]).timestamp() - record["clock"]
    assert delay == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_retry_after_small_value_does_not_lower_backoff(clock, store, counters):
    store.add_sample("evt-1")
    retry = DeliveryOutcome(kind="retry", code="rate_limited", retry_after=0.5)
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [retry], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["attempts"] >= 1)
    await worker.close()

    record = store.record_calls[0]
    delay = datetime.fromisoformat(record["next_retry_at"]).timestamp() - record["clock"]
    assert 2.0 <= delay <= 2.2


@pytest.mark.asyncio
async def test_sample_dead_letters_after_20th_actual_send_failure(clock, store, counters):
    store.add_sample("evt-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [RETRY_500], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(
        worker, lambda: store.rows["evt-1"]["status"] == "dead_letter", max_cycles=3000
    )
    await worker.close()

    row = store.rows["evt-1"]
    assert row["status"] == "dead_letter"
    assert row["attempts"] == SAMPLE_GIVE_UP_ATTEMPTS


@pytest.mark.asyncio
async def test_tombstone_never_gives_up(clock, store, counters):
    store.add_tombstone("deleted:doc-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({TARGET_DELETED_MESSAGE_TYPE: [RETRY_500], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(
        worker,
        lambda: store.rows["deleted:doc-1"]["attempts"] >= SAMPLE_GIVE_UP_ATTEMPTS + 5,
        max_cycles=3000,
    )
    await worker.close()

    row = store.rows["deleted:doc-1"]
    assert row["status"] == "pending"
    assert row["attempts"] >= SAMPLE_GIVE_UP_ATTEMPTS + 5


# ---------------------------------------------------------------------------
# 心跳控制槽
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_sent_immediately_on_start(clock, store, counters):
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: len(requests_of_type(client, STATE_MESSAGE_TYPE)) >= 1, max_cycles=100)
    await worker.close()

    assert len(counters.snapshot_calls) == 1
    payload = parse_body(requests_of_type(client, STATE_MESSAGE_TYPE)[0])
    assert payload["type"] == STATE_MESSAGE_TYPE
    assert payload["sequence"] == 1
    assert payload["observed_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_heartbeat_periodic_at_60s(clock, store, counters):
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    state_requests = []
    task = asyncio.create_task(worker.run())
    for _ in range(600):
        state_requests = requests_of_type(client, STATE_MESSAGE_TYPE)
        if len(state_requests) >= 2:
            break
        await asyncio.sleep(0)
    await worker.close()

    assert len(state_requests) == 2
    delta = state_requests[1]["clock"] - state_requests[0]["clock"]
    # fake sleeper 交错使时钟至多多出一个 interval
    assert 60.0 <= delta <= 65.001
    assert len(counters.snapshot_calls) == 2


@pytest.mark.asyncio
async def test_heartbeat_retry_preserves_snapshot_bytes_and_sequence(clock, store, counters):
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [RETRY_500, ACCEPTED_STATE]}
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    state_requests = []
    task = asyncio.create_task(worker.run())
    for _ in range(600):
        state_requests = requests_of_type(client, STATE_MESSAGE_TYPE)
        if len(state_requests) >= 2:
            break
        await asyncio.sleep(0)
    await worker.close()

    assert len(counters.snapshot_calls) == 1
    assert state_requests[0]["body"] == state_requests[1]["body"]
    assert parse_body(state_requests[0])["sequence"] == 1


@pytest.mark.asyncio
async def test_new_heartbeat_period_replaces_pending_snapshot(clock, store, counters):
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {
                SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE],
                # 前 5 次发送失败（重试与新周期交错），第 6 次由新周期重建的快照成功
                STATE_MESSAGE_TYPE: [RETRY_500] * 5 + [ACCEPTED_STATE],
            }
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    state_requests = []
    task = asyncio.create_task(worker.run())
    for _ in range(4000):
        state_requests = requests_of_type(client, STATE_MESSAGE_TYPE)
        if any(parse_body(entry)["sequence"] == 2 for entry in state_requests):
            break
        await asyncio.sleep(0)
    await worker.close()

    # 新周期到点才重建：旧快照重试期间保留 sequence/正文，替换时生成更高 sequence
    sequences = [parse_body(entry)["sequence"] for entry in state_requests]
    first_new = sequences.index(2)
    assert first_new >= 2
    old_bodies = [entry["body"] for entry in state_requests[:first_new]]
    assert all(body == old_bodies[0] for body in old_bodies)
    assert len(counters.snapshot_calls) == 2
    assert parse_body(state_requests[first_new])["sequence"] == 2
    delta = state_requests[first_new]["clock"] - state_requests[0]["clock"]
    assert delta >= 60.0 - 0.001


@pytest.mark.asyncio
async def test_heartbeat_stale_sequence_discards_and_rebuilds(clock, store, counters):
    stale = DeliveryOutcome(kind="rejected", code="stale_sequence")
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [stale, ACCEPTED_STATE]}
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    state_requests = []
    task = asyncio.create_task(worker.run())
    for _ in range(600):
        state_requests = requests_of_type(client, STATE_MESSAGE_TYPE)
        if len(state_requests) >= 2:
            break
        await asyncio.sleep(0)
    await worker.close()

    assert len(counters.snapshot_calls) == 2
    assert parse_body(state_requests[1])["sequence"] == 2
    delta = state_requests[1]["clock"] - state_requests[0]["clock"]
    # 丢弃后立即重建（时钟交错会多出最多两个 interval），远小于完整心跳周期
    assert 4.999 <= delta <= 20.001


@pytest.mark.asyncio
async def test_heartbeat_not_starved_when_sample_loop_keeps_failing(clock, store, counters):
    for index in range(3):
        store.add_sample(f"evt-{index}")

    def route(body):
        message_type = json.loads(body)["type"]
        if message_type == STATE_MESSAGE_TYPE:
            return ACCEPTED_STATE
        return RETRY_500

    client = FakeDeliveryClient(clock, handler=route)
    # 长 lease：心跳睡眠推进 fake clock 不会使在途批次租约过期（测试口径）
    worker, _ = make_worker(clock, store, counters, client, lease_seconds=1000)

    state_sends = []
    task = asyncio.create_task(worker.run())
    for _ in range(3000):
        state_sends = requests_of_type(client, STATE_MESSAGE_TYPE)
        if len(state_sends) >= 2:
            break
        await asyncio.sleep(0)
    await worker.close()

    assert len(state_sends) >= 2
    assert all(store.rows[f"evt-{index}"]["attempts"] >= 1 for index in range(3))


# ---------------------------------------------------------------------------
# 关闭语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_waits_then_cancels_in_flight_and_keeps_row(clock, store, counters):
    store.add_sample("evt-1")
    gate = asyncio.Event()
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}),
        gate=gate,
    )
    worker, _ = make_worker(clock, store, counters, client)

    task = await run_until(
        worker, lambda: len(requests_of_type(client, SAMPLE_MESSAGE_TYPE)) >= 1, max_cycles=100
    )
    closer = asyncio.create_task(worker.close(wait_seconds=0.2))
    await asyncio.sleep(0.5)
    assert closer.done()
    assert task.cancelled()

    assert "evt-1" in store.rows
    assert store.rows["evt-1"]["attempts"] == 0
    gate.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_close_completes_fast_in_flight_before_cancelling(clock, store, counters):
    store.add_sample("evt-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    task = await run_until(
        worker, lambda: len(requests_of_type(client, SAMPLE_MESSAGE_TYPE)) >= 1, max_cycles=100
    )
    await worker.close(wait_seconds=15)

    assert "evt-1" not in store.rows


# ---------------------------------------------------------------------------
# 心跳与样本循环的异常隔离
# ---------------------------------------------------------------------------


def task_exception(task):
    """读取任务异常；任务被取消时视为无异常（close() 主动取消属正常收尾）。"""
    try:
        return task.exception()
    except asyncio.CancelledError:
        return None


@pytest.mark.asyncio
async def test_heartbeat_survives_snapshot_storage_failure(clock, store, counters):
    flaky = FlakyCounters(fail_times=3)
    store.add_sample("evt-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, flaky, client)

    state_requests = []
    task = asyncio.create_task(worker.run())
    for _ in range(1200):
        state_requests = requests_of_type(client, STATE_MESSAGE_TYPE)
        if "evt-1" not in store.rows and len(state_requests) >= 1:
            break
        await asyncio.sleep(0)
    await worker.close()

    # 样本投递不受心跳存储故障影响
    assert "evt-1" not in store.rows
    # 心跳循环存活并恢复上报（3 次失败后重建成功，sequence 只计成功快照）
    assert len(state_requests) >= 1
    assert flaky.sequence == 1
    # close() 正常完成，run 任务不以无关异常结束（cancelled 视为正常收尾）
    assert task_exception(task) is None


@pytest.mark.asyncio
async def test_heartbeat_survives_persistent_snapshot_storage_failure(clock, store, counters):
    """持续失败场景：重建阶段异常不得引发零睡眠热循环或退避指数溢出杀死任务。

    fail_times 取 2000（远超退避溢出阈值 ~1025）：缺陷形态下重建阶段异常不前推
    next_period，下次迭代零睡眠热循环，failures 无限增长至 2**n 浮点溢出杀死任务。
    """
    flaky = FlakyCounters(fail_times=2000)
    store.add_sample("evt-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, flaky, client)

    task = asyncio.create_task(worker.run())
    # 等到心跳已实际执行多次重建尝试（失败 5 次），再收尾
    for _ in range(2000):
        if flaky.attempts >= 5:
            break
        await asyncio.sleep(0)
    assert flaky.attempts >= 5
    for _ in range(200):
        if "evt-1" not in store.rows:
            break
        await asyncio.sleep(0)
    await worker.close()

    # 样本投递在心跳持续失败期间照常完成
    assert "evt-1" not in store.rows
    # 心跳任务存活：无 OverflowError/热循环致死，run 任务无异常结束
    assert task_exception(task) is None


def test_backoff_caps_exponent_for_large_attempt_counts(clock, store, counters):
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    # 指数限幅：极大失败次数不再触发 2**n 浮点溢出，且退避收敛到 300s 上限
    assert worker._backoff(2000, None) == 300.0
    # 常规路径不受限幅影响
    assert 2.0 <= worker._backoff(1, None) <= 2.2


@pytest.mark.asyncio
async def test_heartbeat_survives_unexpected_client_exception(clock, store, counters):
    def handler(body):
        if json.loads(body)["type"] == STATE_MESSAGE_TYPE:
            raise RuntimeError("client exploded")
        return ACCEPTED_SAMPLE

    client = FakeDeliveryClient(clock, handler=handler)
    worker, _ = make_worker(clock, store, counters, client)

    state_requests = []
    task = asyncio.create_task(worker.run())
    for _ in range(600):
        state_requests = requests_of_type(client, STATE_MESSAGE_TYPE)
        if len(state_requests) >= 2:
            break
        await asyncio.sleep(0)
    await worker.close()

    # 意外客户端异常按 network_error 口径退避，同快照重试
    assert len(state_requests) >= 2
    assert state_requests[0]["body"] == state_requests[1]["body"]
    assert len(counters.snapshot_calls) == 1


@pytest.mark.asyncio
async def test_heartbeat_retry_after_raises_wait_floor(clock, store, counters):
    slow = DeliveryOutcome(kind="retry", code="rate_limited", retry_after=30.0)
    client = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [slow, ACCEPTED_STATE]}
        ),
    )
    worker, _ = make_worker(clock, store, counters, client)

    state_requests = []
    task = asyncio.create_task(worker.run())
    for _ in range(1200):
        state_requests = requests_of_type(client, STATE_MESSAGE_TYPE)
        if len(state_requests) >= 2:
            break
        await asyncio.sleep(0)
    await worker.close()

    assert len(counters.snapshot_calls) == 1
    delta = state_requests[1]["clock"] - state_requests[0]["clock"]
    # 有效 Retry-After（30s）为等待下限：远大于常规退避（~2s），小于心跳周期不替换
    assert 29.999 <= delta <= 40.001


@pytest.mark.asyncio
async def test_unexpected_client_exception_retries_as_network_error(clock, store, counters):
    store.add_sample("evt-1")

    def handler(body):
        if json.loads(body)["type"] == SAMPLE_MESSAGE_TYPE:
            raise RuntimeError("client exploded")
        return ACCEPTED_STATE

    client = FakeDeliveryClient(clock, handler=handler)
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: store.rows["evt-1"]["attempts"] >= 1)
    await worker.close()

    row = store.rows["evt-1"]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["last_error"] == "network_error"
    assert row["next_retry_at"] is not None


# ---------------------------------------------------------------------------
# 租约预算与 claim 异常分类
# ---------------------------------------------------------------------------


def sample_sends_for(client, event_id):
    return [
        entry
        for entry in requests_of_type(client, SAMPLE_MESSAGE_TYPE)
        if json.loads(entry["body"])["producer_event_id"] == event_id
    ]


@pytest.mark.asyncio
async def test_lease_budget_exhaustion_defers_remaining_rows(clock, store, counters):
    store.add_sample("evt-1")
    store.add_sample("evt-2")

    def handler(body):
        # 模拟单次 sample 发送耗时 30s（batch 内第二行剩余预算 45-30=15s 已不足）；
        # 心跳发送不推进时钟，避免干扰租约时序
        if json.loads(body)["type"] == SAMPLE_MESSAGE_TYPE:
            clock.advance(30.0)
            return ACCEPTED_SAMPLE
        return ACCEPTED_STATE

    client = FakeDeliveryClient(clock, handler=handler)
    worker, _ = make_worker(clock, store, counters, client, lease_seconds=45)

    await run_until(worker, lambda: "evt-1" not in store.rows, max_cycles=300)
    # 第一批：evt-1 ACK 后租约预算耗尽，evt-2 本批不发送
    assert sample_sends_for(client, "evt-2") == []

    # 预算恢复（租约过期重领）后 evt-2 恰好投递一次且 ACK 生效
    await run_until(worker, lambda: "evt-2" not in store.rows, max_cycles=800)
    await worker.close()
    assert "evt-2" not in store.rows
    assert len(sample_sends_for(client, "evt-2")) == 1


@pytest.mark.asyncio
async def test_claim_busy_error_retries_next_interval(clock, store, counters):
    store.add_sample("evt-1")
    store.claim_error = sqlite3.OperationalError("database is locked")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: "evt-1" not in store.rows, max_cycles=300)
    await worker.close()
    assert "evt-1" not in store.rows


@pytest.mark.asyncio
async def test_claim_unexpected_operational_error_fails_fast(clock, store, counters):
    store.add_sample("evt-1")
    store.claim_error = sqlite3.OperationalError("no such table: evaluation_outbox")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    task = await run_until(worker, lambda: False, max_cycles=60)
    # 非 busy/locked 类 OperationalError 快速失败抛出，不进入无限告警循环
    assert task.done()
    assert not task.cancelled()
    assert isinstance(task.exception(), sqlite3.OperationalError)
    with pytest.raises(sqlite3.OperationalError):
        await worker.close()


# ---------------------------------------------------------------------------
# 租约竞争与重启重放（真实 SQLiteStore）
# ---------------------------------------------------------------------------


@pytest.fixture
def real_store(tmp_path, monkeypatch, clock):
    store = SQLiteStore(tmp_path / "n-kb.db")
    # record_delivery_attempt 内部使用 _utc_now_iso 校验租约：对齐 fake clock
    monkeypatch.setattr(SQLiteStore, "_utc_now_iso", lambda *args, **kwargs: iso_epoch(clock()))
    return store


@pytest.mark.asyncio
async def test_two_workers_do_not_double_claim(clock, real_store, counters):
    real_store.register_outbox_message(sample_payload("evt-1"), max_rows=100, max_bytes=10**9)
    gate = asyncio.Event()
    client1 = FakeDeliveryClient(
        clock,
        handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}),
        gate=gate,
    )
    client2 = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker1, _ = make_worker(clock, real_store, counters, client1, owner="w1", lease_seconds=10000)
    worker2, _ = make_worker(clock, real_store, counters, client2, owner="w2")

    task1 = await run_until(
        worker1, lambda: len(requests_of_type(client1, SAMPLE_MESSAGE_TYPE)) >= 1, max_cycles=200
    )
    task2 = await run_until(
        worker2, lambda: len(client2.requests) >= 1, max_cycles=100
    )
    # w1 持有租约并阻塞在在途请求上：w2 不得领取同一行（仅心跳在发送）
    assert requests_of_type(client2, SAMPLE_MESSAGE_TYPE) == []

    gate.set()
    for _ in range(100):
        if not _row_exists(real_store, "evt-1"):
            break
        await asyncio.sleep(0)
    await worker1.close()
    await worker2.close()
    task1.cancel()
    task2.cancel()

    assert not _row_exists(real_store, "evt-1")


@pytest.mark.asyncio
async def test_expired_owner_ack_rejected_then_reclaimed(clock, real_store, counters):
    real_store.register_outbox_message(sample_payload("evt-1"), max_rows=100, max_bytes=10**9)
    gate1 = asyncio.Event()
    gate2 = asyncio.Event()
    client1 = FakeDeliveryClient(
        clock,
        handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}),
        gate=gate1,
    )
    client2 = FakeDeliveryClient(
        clock,
        handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}),
        gate=gate2,
    )
    worker1, _ = make_worker(clock, real_store, counters, client1, owner="w1", heartbeat_seconds=5.0)
    worker2, _ = make_worker(clock, real_store, counters, client2, owner="w2")

    task1 = await run_until(
        worker1, lambda: len(requests_of_type(client1, SAMPLE_MESSAGE_TYPE)) >= 1, max_cycles=200
    )
    # w1 持锁阻塞：租约过期后 w2 重新领取同一行（claim 到期重领）
    clock.advance(31)
    task2 = await run_until(
        worker2, lambda: len(requests_of_type(client2, SAMPLE_MESSAGE_TYPE)) >= 1, max_cycles=200
    )
    # 放行 w1 的在途请求：其 ACK 已过期（租约归 w2），回写被拒绝
    gate1.set()
    for _ in range(3):
        await asyncio.sleep(0)

    with real_store._connect() as connection:
        row = connection.execute(
            "SELECT owner, status, attempts FROM evaluation_outbox WHERE producer_event_id = 'evt-1'"
        ).fetchone()
    assert row["owner"] == "w2"
    assert row["status"] == "pending"
    assert row["attempts"] == 0

    await worker1.close()
    gate2.set()
    await worker2.close()
    task1.cancel()
    task2.cancel()


@pytest.mark.asyncio
async def test_restart_replays_same_bytes_after_lease_expiry(clock, real_store, counters):
    real_store.register_outbox_message(sample_payload("evt-1"), max_rows=100, max_bytes=10**9)
    client1 = FakeDeliveryClient(
        clock,
        handler=TypeRouter(
            {SAMPLE_MESSAGE_TYPE: [RETRY_500, ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}
        ),
    )
    worker1, _ = make_worker(clock, real_store, counters, client1, owner="w1")

    await run_until(worker1, lambda: _row_attempts(real_store, "evt-1") == 1, max_cycles=200)
    await worker1.close()
    assert _row_attempts(real_store, "evt-1") == 1

    clock.advance(61)
    client2 = FakeDeliveryClient(
        clock,
        handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]}),
    )
    worker2, _ = make_worker(clock, real_store, counters, client2, owner="w2")
    await run_until(worker2, lambda: not _row_exists(real_store, "evt-1"), max_cycles=200)
    await worker2.close()

    sample1 = requests_of_type(client1, SAMPLE_MESSAGE_TYPE)
    sample2 = requests_of_type(client2, SAMPLE_MESSAGE_TYPE)
    assert not _row_exists(real_store, "evt-1")
    assert sample2[0]["body"] == sample1[0]["body"]


@pytest.mark.asyncio
async def test_accepted_tombstone_marks_remote_acked_and_keeps_row(clock, store, counters):
    store.add_tombstone("deleted:doc-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({TARGET_DELETED_MESSAGE_TYPE: [ACCEPTED_TOMBSTONE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, store, counters, client)

    await run_until(worker, lambda: len(store.mark_remote_acked_calls) >= 1)
    await worker.close()

    # 严格 ACK：置 remote_acked，控制行保留（删除只能由外部清理完成）
    assert len(store.mark_remote_acked_calls) == 1
    assert store.mark_remote_acked_calls[0]["producer_event_id"] == "deleted:doc-1"
    assert store.mark_remote_acked_calls[0]["owner"] == "w1"
    row = store.rows["deleted:doc-1"]
    assert row["status"] == "pending"
    assert row["remote_acked"] == 1
    # 已远端 ACK 的控制行不再被 claim_due_outbox 重领
    assert store.claim_due_outbox(
        "w1", 30, datetime.fromtimestamp(clock(), tz=timezone.utc)
    ) == []


def _row_remote_acked(store, event_id):
    with store._connect() as connection:
        row = connection.execute(
            "SELECT remote_acked FROM evaluation_outbox WHERE producer_event_id = ?", (event_id,)
        ).fetchone()
    return row["remote_acked"] if row is not None else None


@pytest.mark.asyncio
async def test_accepted_tombstone_real_store_marks_remote_acked(clock, real_store, counters):
    real_store.register_control_message(tombstone_payload("doc-1"), control_key="deleted:doc-1")
    client = FakeDeliveryClient(
        clock, handler=TypeRouter({TARGET_DELETED_MESSAGE_TYPE: [ACCEPTED_TOMBSTONE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker, _ = make_worker(clock, real_store, counters, client, owner="w1")

    await run_until(worker, lambda: _row_remote_acked(real_store, "deleted:doc-1") == 1, max_cycles=200)
    await worker.close()

    # 行保留、remote_acked=1、不再被其他 worker 重领
    assert _row_exists(real_store, "deleted:doc-1")
    assert _row_remote_acked(real_store, "deleted:doc-1") == 1
    assert real_store.claim_due_outbox(
        "w2", 30, datetime.fromtimestamp(clock(), tz=timezone.utc)
    ) == []


def _row_exists(store, event_id):
    with store._connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM evaluation_outbox WHERE producer_event_id = ?", (event_id,)
        ).fetchone()
    return row is not None


def _row_attempts(store, event_id):
    with store._connect() as connection:
        row = connection.execute(
            "SELECT attempts FROM evaluation_outbox WHERE producer_event_id = ?", (event_id,)
        ).fetchone()
    return row["attempts"] if row is not None else None


@pytest.mark.asyncio
async def test_normal_restart_keeps_epoch_and_advances_sequence(clock, real_store):
    client1 = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker1, _ = make_worker(clock, real_store, real_store, client1, owner="w1")
    await run_until(
        worker1, lambda: len(requests_of_type(client1, STATE_MESSAGE_TYPE)) >= 1, max_cycles=100
    )
    await worker1.close()

    client2 = FakeDeliveryClient(
        clock, handler=TypeRouter({SAMPLE_MESSAGE_TYPE: [ACCEPTED_SAMPLE], STATE_MESSAGE_TYPE: [ACCEPTED_STATE]})
    )
    worker2, _ = make_worker(clock, real_store, real_store, client2, owner="w2")
    await run_until(
        worker2, lambda: len(requests_of_type(client2, STATE_MESSAGE_TYPE)) >= 1, max_cycles=100
    )
    await worker2.close()

    first = parse_body(requests_of_type(client1, STATE_MESSAGE_TYPE)[0])
    second = parse_body(requests_of_type(client2, STATE_MESSAGE_TYPE)[0])
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert second["counter_epoch"] == first["counter_epoch"]
