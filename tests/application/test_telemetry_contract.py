"""Normalized contract and lifecycle regression tests."""

import asyncio
import importlib.util
from types import MappingProxyType

import pytest
from opentelemetry import trace
from app.application import telemetry_semantics as ts, telemetry_details as details


def test_contract_is_explicit_immutable_and_strict():
    assert importlib.util.find_spec("app.application.telemetry_contract") is not None
    from app.application import telemetry_contract as c

    assert c.OUTCOMES == frozenset(
        (
            "success",
            "failure",
            "denied",
            "cancelled",
            "timed_out",
            "skipped",
            "deferred",
            "unknown",
        )
    )
    assert len(c.CONTENT_FIELDS) == 11
    assert len(c.INSTRUMENTS) == 16
    assert c.INSTRUMENTS["nkb.retrieval.results"].boundaries[0] == 0
    with pytest.raises(TypeError):
        c.OPERATIONS["bad"] = None
    for value in (True, -1, "2", None):
        assert c.project_attributes({"nkb.index.chunk_count": value}) == {}
    for value in (True, -1, float("inf"), float("nan")):
        assert c.project_attributes({"nkb.index.duration_ms": value}) == {}
    assert c.project_attributes(
        {"nkb.index.chunk_count": 0, "nkb.metric.overflow": False}
    ) == {"nkb.index.chunk_count": 0, "nkb.metric.overflow": False}
    assert (
        c.project_attributes(
            {
                "nkb.outcome": "success",
                "nkb.index.secret": "private",
                "nkb.document.id": "a" * 129,
            }
        )
        == {}
    )


def test_completion_snapshot_shared_frozen_and_current(monkeypatch, caplog):
    assert hasattr(ts, "configure_completion")
    snapshots = []
    measured = []

    class Span(trace.NonRecordingSpan):
        def __init__(self):
            super().__init__(trace.INVALID_SPAN_CONTEXT)
            self.attrs = {}
            self.events = []

        def set_attribute(self, k, v):
            self.attrs[k] = v

        def set_attributes(self, attributes):
            self.attrs.update(attributes)

        def add_event(self, n, attributes):
            self.events.append((n, dict(attributes)))

        def get_span_context(self):
            return trace.INVALID_SPAN_CONTEXT

        def end(self):
            pass

    span = Span()
    monkeypatch.setattr(ts, "_start_span", lambda *a: span)
    monkeypatch.setattr(
        ts, "_counter_add", lambda *a: measured.append(trace.get_current_span())
    )
    ts.configure_completion(
        lambda snapshot: snapshots.append((snapshot, trace.get_current_span()))
    )
    try:
        with ts.retrieval_span():
            assert "nkb.retrieval.outcome" not in ts.current_operation_context()
            ts.record_retrieval("success", result_count=0)
            ts.record_retrieval("success", result_count=0)
        assert len(snapshots) == 1
        snapshot, current = snapshots[0]
        assert current is span and measured == [span]
        assert snapshot.event == "nkb.retrieval.completed"
        assert snapshot.attributes == span.attrs == span.events[0][1]
        assert snapshot.attributes["nkb.retrieval.result_count"] == 0
        with pytest.raises(TypeError):
            snapshot.attributes["nkb.retrieval.outcome"] = "failure"
    finally:
        ts.configure_completion(None)


@pytest.mark.parametrize(
    "exc,outcome",
    [
        (GeneratorExit(), "cancelled"),
        (TimeoutError(), "timed_out"),
        (ValueError("x"), "failure"),
    ],
)
def test_new_exception_outcomes(exc, outcome):
    assert ts.outcome_for_exception(exc) == outcome
    if outcome == "cancelled":
        assert ts.error_type_for_exception(exc) is None


def test_trusted_timeout_chain_preserves_wrapper():
    Timeout = type("ReadTimeout", (Exception,), {"__module__": "httpx"})
    inner = Timeout("secret")
    outer = RuntimeError("embedding_provider_failed")
    outer.__cause__ = inner
    assert ts.outcome_for_exception(outer) == "timed_out"
    assert ts.error_type_for_exception(outer) == "timeout"
    assert ts.outcome_for_exception(RuntimeError("timeout")) == "failure"


def test_context_inherits_identity_only_and_restores():
    with ts.operation_context(
        **{
            "nkb.request.channel": "api",
            "nkb.request.id": "req-1",
            "nkb.index.outcome": "failure",
            "nkb.index.summary": "private",
            "operation": "bad",
        }
    ):
        parent = dict(ts.current_operation_context())
        with ts.index_stage_span("embed"):
            child = ts.current_operation_context()
            assert child["nkb.request.id"] == "req-1"
            assert child["nkb.index.operation"] == "embed"
            assert "nkb.index.outcome" not in child and "nkb.index.summary" not in child
        assert ts.current_operation_context() == parent
    assert ts.current_operation_context() == {}


def test_details_flatten_canonical_and_metadata_types():
    details.configure_details(lambda text: text)
    try:
        flat = details.flatten_details(details.project_details(retrieval_input="safe"))
        assert flat["nkb.content.retrieval_input"] == "safe"
        assert flat["nkb.content.retrieval_input_original_bytes"] == 4
        assert not any(key.startswith("nkb.details.") for key in flat)
    finally:
        details.configure_details(None)


def test_huge_duration_is_omitted_without_raising():
    from app.application import telemetry_contract as c

    assert c.project_attributes({"nkb.index.duration_ms": 10**1000}) == {}


def test_detail_failure_does_not_suppress_completion(monkeypatch):
    snapshots = []
    ts.configure_completion(snapshots.append)

    def broken(**kwargs):
        raise RuntimeError("observation fault")

    monkeypatch.setattr(details, "update_current_details", broken)
    error = TimeoutError("business original")
    try:
        with pytest.raises(TimeoutError) as caught:
            with ts.retrieval_span():
                raise error
        assert caught.value is error
        assert len(snapshots) == 1
        assert snapshots[0].attributes["nkb.retrieval.outcome"] == "timed_out"
    finally:
        ts.configure_completion(None)


def test_known_tokens_can_be_cleared_to_unknown():
    snapshots = []
    ts.configure_completion(snapshots.append)
    try:
        with ts.embedding_observer("model") as scope:
            scope.set_input_tokens(7)
            scope.set_input_tokens(None)
        assert "gen_ai.usage.input_tokens" not in snapshots[0].attributes
        assert snapshots[0].attributes["nkb.model.usage_known"] is False
    finally:
        ts.configure_completion(None)


def test_completion_default_log_uses_identical_snapshot(monkeypatch, caplog):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, "_get_tracer", lambda: provider.get_tracer("snapshot"))
    caplog.set_level("INFO", logger=ts.__name__)
    try:
        with ts.embedding_observer("model") as scope:
            scope.set_input_tokens(0)
        span = exporter.get_finished_spans()[0]
        records = [
            record
            for record in caplog.records
            if getattr(record, "nkb_completion_owned", False)
        ]
        assert len(records) == 1 and len(span.events) == 1
        snapshot = records[0].nkb_completion_snapshot
        assert snapshot.attributes == span.attributes == span.events[0].attributes
        assert snapshot.trace_id == f"{span.context.trace_id:032x}"
        assert snapshot.span_id == f"{span.context.span_id:016x}"
        assert snapshot.attributes["gen_ai.usage.input_tokens"] == 0
        assert snapshot.attributes["nkb.model.usage_known"] is True
        assert "nkb.model.operation" not in snapshot.attributes
        assert snapshot.attributes["gen_ai.provider.name"] == "ollama"
    finally:
        provider.shutdown()


def test_nested_concurrent_children_restore_facts_and_details():
    snapshots = []
    ts.configure_completion(snapshots.append)
    details.configure_details(lambda text: text)

    async def child(count):
        with ts.retrieval_span():
            ts.record_retrieval("success", result_count=count)
            details.update_current_details(retrieval_input=str(count))
            await asyncio.sleep(0)

    async def run():
        with ts.index_run_span("doc-1"):
            details.update_current_details(index_input="parent")
            await asyncio.gather(child(0), child(1))
            assert ts.current_operation_context()["nkb.index.operation"] == "run"
            assert "nkb.retrieval.result_count" not in ts.current_operation_context()
            assert set(details.current_details()) == {"index_input"}

    try:
        asyncio.run(run())
        children = [s for s in snapshots if s.event == "nkb.retrieval.completed"]
        assert {s.attributes["nkb.retrieval.result_count"] for s in children} == {0, 1}
        for snapshot in children:
            assert snapshot.attributes["nkb.index.document_id"] == "doc-1"
            assert "nkb.content.index_input" not in snapshot.attributes
            assert snapshot.attributes["nkb.content.retrieval_input"] == str(
                snapshot.attributes["nkb.retrieval.result_count"]
            )
        assert ts.current_operation_context() == {} and details.current_details() == {}
    finally:
        ts.configure_completion(None)
        details.configure_details(None)


def test_all_sixteen_instruments_match_preserved_contract():
    from app.application import telemetry_contract as c

    duration = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
    genai = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.075,
        0.1,
        0.25,
        0.5,
        0.75,
        1.0,
        2.5,
        5.0,
        7.5,
        10.0,
        30.0,
    )
    token = (
        1,
        4,
        16,
        64,
        256,
        1024,
        4096,
        16384,
        65536,
        262144,
        1048576,
        4194304,
        16777216,
        67108864,
    )
    expected = {
        "nkb.document.uploads": ("counter", "1", (), ("nkb.document.outcome",)),
        "nkb.document.upload.duration": (
            "histogram",
            "s",
            duration,
            ("nkb.document.outcome",),
        ),
        "nkb.index.jobs": ("counter", "1", (), ("nkb.index.outcome",)),
        "nkb.index.duration": ("histogram", "s", duration, ("nkb.index.outcome",)),
        "nkb.index.chunks": (
            "histogram",
            "{chunk}",
            (1, 2, 4, 8, 16, 32, 64, 128, 256, 512),
            (),
        ),
        "nkb.index.stage.duration": (
            "histogram",
            "s",
            duration,
            ("nkb.index.stage", "nkb.index.outcome"),
        ),
        "nkb.retrieval.requests": ("counter", "1", (), ("nkb.retrieval.outcome",)),
        "nkb.retrieval.duration": (
            "histogram",
            "s",
            duration,
            ("nkb.retrieval.outcome",),
        ),
        "nkb.retrieval.results": (
            "histogram",
            "{result}",
            (0, 1, 2, 3, 5, 8, 10, 20, 50, 100),
            (),
        ),
        "nkb.mcp.requests": (
            "counter",
            "1",
            (),
            ("nkb.mcp.operation", "nkb.mcp.outcome"),
        ),
        "nkb.mcp.duration": (
            "histogram",
            "s",
            duration,
            ("nkb.mcp.operation", "nkb.mcp.outcome"),
        ),
        "gen_ai.client.operation.duration": (
            "histogram",
            "s",
            genai,
            ("gen_ai.operation.name", "gen_ai.request.model", "error.type"),
        ),
        "gen_ai.client.token.usage": (
            "histogram",
            "{token}",
            token,
            ("gen_ai.operation.name", "gen_ai.request.model", "gen_ai.token.type"),
        ),
        "nkb.model.cost": (
            "counter",
            "USD",
            (),
            ("gen_ai.operation.name", "nkb.model.billing_provider", "nkb.model.billing_model"),
        ),
        "nkb.model.cost_components": (
            "counter",
            "USD",
            (),
            (
                "gen_ai.operation.name",
                "nkb.model.billing_provider",
                "nkb.model.billing_model",
                "nkb.model.cost_component",
            ),
        ),
        "nkb.model.cost_attempts": (
            "counter",
            "{attempt}",
            (),
            (
                "gen_ai.operation.name",
                "nkb.model.billing_provider",
                "nkb.model.billing_model",
                "nkb.model.cost_status",
                "nkb.model.cost_reason",
            ),
        ),
    }
    assert {
        name: (s.kind, s.unit, s.boundaries, s.allowed_dimensions)
        for name, s in c.INSTRUMENTS.items()
    } == expected


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "failure",
        "denied",
        "cancelled",
        "timed_out",
        "skipped",
        "deferred",
        "unknown",
    ],
)
def test_explicit_terminal_outcomes_set_status_only_for_errors(monkeypatch, outcome):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, "_get_tracer", lambda: provider.get_tracer("outcomes"))
    try:
        with ts.index_run_span():
            ts.set_operation_result(outcome)
        span = exporter.get_finished_spans()[0]
        assert span.attributes["nkb.index.outcome"] == outcome
        assert (span.status.status_code == trace.StatusCode.ERROR) == (
            outcome in ("failure", "timed_out")
        )
        if outcome == "cancelled":
            assert "error.type" not in span.attributes
    finally:
        provider.shutdown()


def test_unprintable_business_exception_still_completes_once(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    class UnprintableError(Exception):
        def __str__(self):
            raise RuntimeError("broken exception formatting")

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, "_get_tracer", lambda: provider.get_tracer("unprintable"))
    measurements = []
    monkeypatch.setattr(ts, "_counter_add", lambda *args: measurements.append(args))
    snapshots = []
    ts.configure_completion(snapshots.append)
    error = UnprintableError()
    try:
        with pytest.raises(UnprintableError) as caught:
            with ts.retrieval_span():
                raise error
        assert caught.value is error
        assert len(snapshots) == len(measurements) == 1
        span = exporter.get_finished_spans()[0]
        assert len(span.events) == 1
        assert snapshots[0].attributes == span.attributes == span.events[0].attributes
        assert snapshots[0].attributes["nkb.retrieval.outcome"] == "failure"
        assert snapshots[0].attributes["error.type"] == "other"
    finally:
        ts.configure_completion(None)
        provider.shutdown()


def test_frozen_duration_uses_same_elapsed_seconds_for_metric(monkeypatch):
    from types import SimpleNamespace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, "_get_tracer", lambda: provider.get_tracer("duration"))
    ticks = iter((10.0, 10.25))
    monkeypatch.setattr(ts, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    measurements = []
    monkeypatch.setattr(
        ts,
        "_histogram_record",
        lambda name, value, dims: measurements.append(
            (name, value, dict(dims), trace.get_current_span())
        ),
    )
    snapshots = []
    ts.configure_completion(snapshots.append)
    try:
        with ts.retrieval_span() as current:
            ts.record_retrieval("success", result_count=0, seconds=999)
        duration = next(
            item for item in measurements if item[0] == "nkb.retrieval.duration"
        )
        assert duration[1] == 0.25
        assert duration[3] is current
        span = exporter.get_finished_spans()[0]
        snapshot = snapshots[0]
        assert snapshot.attributes["nkb.retrieval.duration_ms"] == 1000 * duration[1]
        assert snapshot.attributes == span.attributes == span.events[0].attributes
    finally:
        ts.configure_completion(None)
        provider.shutdown()


def test_all_eleven_content_fields_and_metadata_flatten_exactly():
    from app.application import telemetry_contract as c

    expected_fields = {
        "retrieval_input",
        "retrieval_output",
        "document_input",
        "index_input",
        "index_result",
        "embedding_input",
        "embedding_result",
        "http_request",
        "http_response",
        "error",
        "log_fields",
    }
    assert c.CONTENT_FIELDS == expected_fields == details.FIELDS
    metadata = {
        "original_bytes": 4,
        "truncated": False,
        "incomplete": False,
        "observed_bytes": 4,
    }
    details.configure_details(lambda text: text)
    try:
        envelopes = details.project_details(
            **{field: "safe" for field in expected_fields}
        )
        for envelope in envelopes.values():
            envelope.update(metadata)
            envelope.seal()
        flattened = details.flatten_details(envelopes)
        expected = {f"nkb.content.{field}": "safe" for field in expected_fields}
        expected.update(
            {
                f"nkb.content.{field}_{suffix}": value
                for field in expected_fields
                for suffix, value in metadata.items()
            }
        )
        assert flattened == expected
        assert c.project_attributes(flattened) == expected
        for field in expected_fields:
            for suffix in metadata:
                invalid = "4" if suffix.endswith("bytes") else 0
                assert (
                    c.project_attributes({f"nkb.content.{field}_{suffix}": invalid})
                    == {}
                )
    finally:
        details.configure_details(None)


def test_general_facts_cannot_bypass_content_policy(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        ts, "_get_tracer", lambda: provider.get_tracer("content-policy")
    )
    details.configure_details(None)
    snapshots = []
    ts.configure_completion(snapshots.append)
    raw = "private-raw-content" * 1000
    try:
        with ts.operation_context(
            **{"nkb.content.error": raw, "nkb.content.error_original_bytes": len(raw)}
        ):
            assert not any(
                key.startswith("nkb.content.") for key in ts.current_operation_context()
            )
            with ts.retrieval_span() as span:
                ts.set_span_attribute(span, "nkb.content.error", raw)
                ts.set_span_attribute(
                    span, "nkb.content.error_original_bytes", len(raw)
                )
                ts._submit(**{"nkb.content.error": raw})
        recorded = exporter.get_finished_spans()[0]
        assert not any(key.startswith("nkb.content.") for key in recorded.attributes)
        assert not any(
            key.startswith("nkb.content.") for key in snapshots[0].attributes
        )
        assert (
            snapshots[0].attributes
            == recorded.attributes
            == recorded.events[0].attributes
        )
    finally:
        ts.configure_completion(None)
        provider.shutdown()


def test_unknown_nested_scope_cannot_mutate_parent_result():
    snapshots = []
    ts.configure_completion(snapshots.append)
    try:
        with ts.operation_context(**{"nkb.request.id": "req-1"}):
            with ts.retrieval_span():
                before = dict(ts.current_operation_context())
                with ts.operation_span("unknown.operation"):
                    assert ts.current_operation_context() == {
                        "nkb.request.id": "req-1",
                        "nkb.observation.error": "unknown_operation",
                    }
                    ts.set_operation_result("denied")
                    ts.record_retrieval("failure", result_count=99)
                    ts._submit(**{"nkb.retrieval.result_count": 99})
                assert ts.current_operation_context() == before
        assert len(snapshots) == 1
        assert snapshots[0].attributes["nkb.retrieval.outcome"] == "success"
        assert "nkb.retrieval.result_count" not in snapshots[0].attributes
    finally:
        ts.configure_completion(None)


# ---------------------------------------------------------------------------
# T6: 成本属性合同登记（键名是 T5 KB 管线输入，不得漂移）
# ---------------------------------------------------------------------------


def test_cost_attribute_names_are_locked():
    from app.application import telemetry_contract as c

    assert c.NKB_MODEL_COST_STATUS == "nkb.model.cost_status"
    assert c.NKB_MODEL_COST_REASON == "nkb.model.cost_reason"
    assert c.NKB_MODEL_COST_CURRENCY == "nkb.model.cost_currency"
    assert c.NKB_MODEL_COST_USD == "nkb.model.cost_usd"
    assert c.NKB_MODEL_COST_DECIMAL == "nkb.model.cost_decimal"
    assert c.NKB_MODEL_COST_INPUT_USD == "nkb.model.cost_input_usd"
    assert c.NKB_MODEL_PRICING_VERSION == "nkb.model.pricing_version"
    assert c.NKB_MODEL_PRICE_ID == "nkb.model.price_id"
    assert c.NKB_MODEL_BILLING_PROVIDER == "nkb.model.billing_provider"
    assert c.NKB_MODEL_BILLING_MODEL == "nkb.model.billing_model"
    assert c.INSTRUMENT_MODEL_COST == "nkb.model.cost"
    assert c.INSTRUMENT_MODEL_COST_COMPONENTS == "nkb.model.cost_components"
    assert c.INSTRUMENT_MODEL_COST_ATTEMPTS == "nkb.model.cost_attempts"


def test_cost_attribute_kinds_and_validation():
    from app.application import telemetry_contract as c

    assert c.ATTRIBUTES[c.NKB_MODEL_COST_STATUS] == "cost_status"
    assert c.ATTRIBUTES[c.NKB_MODEL_COST_REASON] == "cost_reason"
    assert c.ATTRIBUTES[c.NKB_MODEL_COST_CURRENCY] == "cost_currency"
    assert c.ATTRIBUTES[c.NKB_MODEL_COST_USD] == "number"
    assert c.ATTRIBUTES[c.NKB_MODEL_COST_INPUT_USD] == "number"
    assert c.ATTRIBUTES[c.NKB_MODEL_COST_DECIMAL] == "decimal"
    assert c.ATTRIBUTES[c.NKB_MODEL_PRICING_VERSION] == "pricing_version"
    assert c.ATTRIBUTES[c.NKB_MODEL_PRICE_ID] == "price_id"
    assert c.ATTRIBUTES[c.NKB_MODEL_BILLING_PROVIDER] == "billing_provider"
    assert c.ATTRIBUTES[c.NKB_MODEL_BILLING_MODEL] == "billing_model"

    assert c.valid_attribute(c.NKB_MODEL_COST_STATUS, "estimated")
    assert c.valid_attribute(c.NKB_MODEL_COST_STATUS, "unknown")
    assert not c.valid_attribute(c.NKB_MODEL_COST_STATUS, "other")
    assert not c.valid_attribute(c.NKB_MODEL_COST_STATUS, True)

    for reason in (
        "missing_usage",
        "invalid_usage",
        "price_not_found",
        "price_ambiguous",
        "unsupported_pricing",
        "identity_unresolved",
        "pricing_unavailable",
        "amount_unrepresentable",
        "none",
    ):
        assert c.valid_attribute(c.NKB_MODEL_COST_REASON, reason), reason
    assert not c.valid_attribute(c.NKB_MODEL_COST_REASON, "other")
    assert not c.valid_attribute(c.NKB_MODEL_COST_REASON, "missing")

    assert c.valid_attribute(c.NKB_MODEL_COST_CURRENCY, "USD")
    assert not c.valid_attribute(c.NKB_MODEL_COST_CURRENCY, "EUR")

    assert c.valid_attribute(c.NKB_MODEL_COST_USD, 0.00002)
    assert c.valid_attribute(c.NKB_MODEL_COST_USD, 0)
    assert not c.valid_attribute(c.NKB_MODEL_COST_USD, -1)
    assert not c.valid_attribute(c.NKB_MODEL_COST_USD, float("nan"))
    assert not c.valid_attribute(c.NKB_MODEL_COST_USD, True)

    assert c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "0.00002")
    assert c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "0")
    # 规范非指数十进制，逐侧 340 位（与 Domain 投影上限、Collector 护栏一致）
    assert c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "0." + "0" * 75 + "1")
    assert c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "1" * 340)
    assert c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "9" * 340 + "." + "3" * 340)
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "1" * 341)
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "0." + "0" * 340 + "1")
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "1e400abc")
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "1e-5")
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, ".5")
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "-1")
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, 0.5)
    assert not c.valid_attribute(c.NKB_MODEL_COST_DECIMAL, "x" * 65)

    assert c.valid_attribute(c.NKB_MODEL_PRICING_VERSION, "2026-11-09-init@" + "a" * 64)
    assert not c.valid_attribute(c.NKB_MODEL_PRICING_VERSION, "")
    assert not c.valid_attribute(c.NKB_MODEL_PRICING_VERSION, "v" * 130)

    assert c.valid_attribute(c.NKB_MODEL_PRICE_ID, "price-1")
    assert not c.valid_attribute(c.NKB_MODEL_PRICE_ID, "")
    assert not c.valid_attribute(c.NKB_MODEL_PRICE_ID, "p" * 129)

    assert c.valid_attribute(c.NKB_MODEL_BILLING_PROVIDER, "ollama")
    assert c.valid_attribute(c.NKB_MODEL_BILLING_PROVIDER, "other")
    assert not c.valid_attribute(c.NKB_MODEL_BILLING_PROVIDER, "openai")

    assert c.valid_attribute(c.NKB_MODEL_BILLING_MODEL, "bge-m3")
    assert not c.valid_attribute(c.NKB_MODEL_BILLING_MODEL, "")
    assert not c.valid_attribute(c.NKB_MODEL_BILLING_MODEL, "m" * 65)

    assert c.project_attributes(
        {
            c.NKB_MODEL_COST_STATUS: "estimated",
            c.NKB_MODEL_COST_USD: 0.00002,
            c.NKB_MODEL_COST_DECIMAL: "0.00002",
            c.NKB_MODEL_BILLING_MODEL: "bge-m3",
        }
    ) == {
        c.NKB_MODEL_COST_STATUS: "estimated",
        c.NKB_MODEL_COST_USD: 0.00002,
        c.NKB_MODEL_COST_DECIMAL: "0.00002",
        c.NKB_MODEL_BILLING_MODEL: "bge-m3",
    }
