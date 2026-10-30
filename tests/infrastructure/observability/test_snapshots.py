"""Runtime snapshots exercise real SDK aggregations, never SDK objects."""
import json
from types import SimpleNamespace


def test_real_sdk_histogram_counter_projection_and_copy():
    from app.infrastructure.observability import snapshots
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(__name__)
    meter.create_histogram('duration', unit='s').record(2, {'outcome': 'success', 'query': 'SECRET'})
    meter.create_counter('requests').add(3)
    registry = {n: SimpleNamespace(kind=k, unit=u, allowed_dimensions=('outcome',))
                for n,k,u in [('duration','histogram','s'), ('requests','counter','')]}
    summary = snapshots.MetricSnapshot(registry)
    summary.capture(reader.get_metrics_data())
    result = summary.latest()
    assert result['collection_count'] == 1
    histogram, counter = result['instruments']
    assert histogram['points'][0]['count'] == 1
    assert histogram['points'][0]['sum'] == 2
    assert histogram['points'][0]['attributes'] == {'outcome': 'success'}
    assert counter['points'][0]['value'] == 3
    assert histogram['points'][0]['end_time'].endswith('+00:00')
    assert 'SECRET' not in json.dumps(result, allow_nan=False)
    result['instruments'].clear()
    assert len(summary.latest()['instruments']) == 2
    summary.capture(object())
    assert summary.latest()['stale'] is True
    assert len(summary.latest()['instruments']) == 2
    provider.shutdown()


def test_log_buffer_limits_order_copy_and_clear():
    from app.infrastructure.observability import snapshots
    buffer = snapshots.SafeLogBuffer()
    for index in range(105):
        buffer.append({'timestamp':str(index), 'severity':'INFO', 'event':'completed',
                       'trace_id':None, 'span_id':None, 'attributes':{}, 'message':'SECRET'})
    result = buffer.latest()
    assert result['capacity'] == result['retained_count'] == 100
    assert result['evicted_count'] == 5
    assert result['returned_count'] == 10
    assert result['items'][0]['timestamp'] == '104'
    assert 'SECRET' not in json.dumps(result)
    result['items'][0]['attributes']['secret'] = 'SECRET'
    assert buffer.latest()['items'][0]['attributes'] == {}
    buffer.clear()
    assert buffer.latest()['retained_count'] == 0


def test_snapshots_fail_closed_on_values_and_enforce_limits():
    from app.infrastructure.observability.snapshots import MetricSnapshot, SafeLogBuffer
    from opentelemetry.sdk.metrics.export import MetricsData, ResourceMetrics, ScopeMetrics, Metric, Sum, NumberDataPoint, AggregationTemporality
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    points = [NumberDataPoint({'outcome':'SECRET', 'device':'/SECRET', 'otel.metrics.overflow': True}, 1, 2, float('nan')) for _ in range(105)]
    metric = Metric('requests', '', '1', Sum(points, AggregationTemporality.CUMULATIVE, True))
    data = MetricsData([ResourceMetrics(Resource({}), [ScopeMetrics(InstrumentationScope('test'), [metric], '')], '')])
    snapshot = MetricSnapshot({'requests':SimpleNamespace(kind='counter',unit='1',allowed_dimensions=('outcome',))})
    snapshot.capture(data)
    result = snapshot.latest()
    assert result['truncated'] is True
    assert len(result['instruments'][0]['points']) == 100
    assert result['instruments'][0]['points'][0]['value'] is None
    assert result['instruments'][0]['points'][0]['attributes'] == {'otel.metrics.overflow':True}
    buffer = SafeLogBuffer()
    buffer.append({'attributes':{'query':'SECRET','exception.message':'SECRET'}})
    assert 'SECRET' not in json.dumps(buffer.latest(), allow_nan=False)


def test_system_series_aggregate_after_private_dimensions_removed():
    from app.infrastructure.observability.snapshots import MetricSnapshot
    from opentelemetry.sdk.metrics.export import MetricsData, ResourceMetrics, ScopeMetrics, Metric, Gauge, NumberDataPoint
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    points = [NumberDataPoint({'cpu': cpu, 'state': 'user'}, 1, 2, value) for cpu, value in [(0, .2), (1, .4)]]
    metric = Metric('system.cpu.utilization', '', '1', Gauge(points))
    data = MetricsData([ResourceMetrics(Resource({}), [ScopeMetrics(InstrumentationScope('test'), [metric], '')], '')])
    snapshot = MetricSnapshot()
    snapshot.capture(data)
    values = snapshot.latest()['instruments'][0]['points']
    assert len(values) == 1
    assert abs(values[0]['value'] - .3) < .0001
    snapshot.clear()
    snapshot.capture(data)
    assert snapshot.latest()['status'] == 'disabled'
    assert snapshot.latest()['instruments'] == []


def test_concurrent_snapshot_readers_receive_independent_consistent_values():
    from concurrent.futures import ThreadPoolExecutor
    from app.infrastructure.observability.snapshots import SafeLogBuffer, MetricSnapshot
    logs, metrics = SafeLogBuffer(), MetricSnapshot()
    def work(index):
        logs.append({'event':'diagnostic_log','attributes':{'result_count':index}})
        if index % 2:
            metrics.capture(object())
        state = logs.latest()
        assert state['returned_count'] == len(state['items'])
        assert state['retained_count'] <= state['capacity']
        state['items'].clear()
        assert metrics.latest()['collection_count'] >= 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(200)))
    assert logs.latest()['retained_count'] == 100


def test_real_span_ring_keeps_facts_even_when_projector_returns_content():
    from opentelemetry.sdk.trace import TracerProvider
    from app.infrastructure.observability.setup import _RingBufferSpanProcessor
    ring = _RingBufferSpanProcessor({'attribute_projector':lambda attrs:dict(attrs)})
    provider = TracerProvider()
    provider.add_span_processor(ring)
    with provider.get_tracer('ring').start_as_current_span('safe-operation') as span:
        span.set_attribute('nkb.details.retrieval_input.content','body-sentinel')
        span.set_attribute('nkb.details.retrieval_input.original_bytes',13)
        span.set_attribute('message','message-sentinel')
        span.set_attribute('exception.stacktrace','stack-sentinel')
        span.set_attribute('http.request.method','POST')
    snapshot = ring.snapshot_spans()
    assert snapshot[0]['attributes']['http.request.method'] == 'POST'
    assert 'sentinel' not in json.dumps(snapshot)
    assert not any(key.startswith('nkb.details.') for key in snapshot[0]['attributes'])
    provider.shutdown()
