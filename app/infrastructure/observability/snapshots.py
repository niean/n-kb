"""Bounded JSON projections. No SDK records or content survive collection."""
from __future__ import annotations

import copy
import json
import math
import re
import threading
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace

_execution_projector = None


def configure_execution_projector(projector=None):
    global _execution_projector
    _execution_projector = projector


_ENUMS = {
    'state': {'user','system','idle','nice','iowait','irq','softirq','steal','interrupt','dpc','free','used','cached','buffered','shared','slab','inactive','active','wired'},
    'type': {'rss','vms','shared','text','lib','data','dirty','user','system','children_user','children_system','voluntary','involuntary'},
    'direction': {'read','write','receive','transmit'},
}

# Explicit system instruments; device/interface/address dimensions are excluded.
SYSTEM_INSTRUMENTS = {
    name: SimpleNamespace(kind=kind, unit=unit, allowed_dimensions=dims)
    for name,kind,unit,dims in (
        ('system.cpu.time','counter','s',('state',)),
        ('system.cpu.utilization','gauge','1',('state',)),
        ('system.memory.usage','gauge','By',('state',)),
        ('system.memory.utilization','gauge','1',('state',)),
        ('system.swap.usage','gauge','pages',('state',)),
        ('system.swap.utilization','gauge','1',('state',)),
        ('system.disk.io','counter','By',('direction',)),
        ('system.network.io','counter','By',('direction',)),
        ('system.thread_count','gauge','',()),
        ('process.cpu.time','counter','s',('type',)),
        ('process.cpu.utilization','gauge','1',()),
        ('process.memory.usage','up_down_counter','By',()),
        ('process.memory.virtual','up_down_counter','By',()),
        ('process.thread.count','up_down_counter','',()),
    )
}


def _execution(attributes):
    if _execution_projector is None:
        return {}
    try:
        return dict(_execution_projector(attributes))
    except Exception:
        return {}


def safe_dimensions(attributes, dimensions):
    facts = _execution(attributes)
    facts.update({key:value for key,value in attributes.items()
                  if key in _ENUMS and type(value) is str and value in _ENUMS[key]})
    return {key:value for key,value in facts.items() if key in dimensions}


def project_log_attributes(attributes):
    """Strict execution facts and pinned standard transport facts; no content."""
    if not isinstance(attributes, dict) and not hasattr(attributes, 'items'):
        return {}
    projected = _execution(attributes)
    status = attributes.get('http.response.status_code')
    if type(status) is int and 100 <= status <= 599:
        projected['http.response.status_code'] = status
    method = attributes.get('http.request.method')
    if method in ('GET','HEAD','OPTIONS','POST','PUT','PATCH','DELETE','CONNECT','TRACE'):
        projected['http.request.method'] = method
    host = attributes.get('server.address')
    if type(host) is str and len(host) <= 253 and re.fullmatch(r'[A-Za-z0-9.:-]+', host):
        projected['server.address'] = host
    port = attributes.get('server.port')
    if type(port) is int and 0 < port <= 65535:
        projected['server.port'] = port
    for key in ('http.route', 'url.template'):
        value = attributes.get(key)
        if type(value) is str and value.startswith('/') and len(value) <= 512 and '?' not in value and '#' not in value:
            from .instrumentors import _http_route_template
            projected[key] = _http_route_template(value)
    return projected


def project_span_facts(attributes):
    """Self inspection retains fixed summaries and facts, never body text."""
    from .instrumentors import ALLOWED_AUTO_ATTRIBUTES, _filter_attributes
    result = _filter_attributes({key:value for key,value in attributes.items()
                                 if key in ALLOWED_AUTO_ATTRIBUTES})
    result.update(_execution(attributes))
    return result


def _utc(nanos=None):
    return datetime.now(timezone.utc).isoformat() if nanos is None else datetime.fromtimestamp(nanos / 1e9, timezone.utc).isoformat()


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


class MetricSnapshot:
    def __init__(self, registry=None, attribute_projector=None, collection_interval_millis=15000):
        self._registry = {**SYSTEM_INSTRUMENTS, **dict(registry or {})}
        self._project = attribute_projector
        self._closed = False
        self._lock = threading.Lock()
        self._state = dict(status='unknown', collected_at=None, collection_count=0,
                           error=None, truncated=False, truncation_scope=[],
                           collection_interval_millis=collection_interval_millis,
                           last_success_at=None, stale=False, instruments=[])

    def capture(self, metrics_data):
        with self._lock:
            if self._closed:
                return
        error = None
        truncation_scope = []
        truncated = False
        instruments = []
        try:
            for rm in metrics_data.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        spec = self._registry.get(metric.name)
                        if spec is None:
                            continue
                        if len(instruments) >= 32:
                            truncated = True
                            truncation_scope.append('instruments')
                            continue
                        data = metric.data
                        kind = type(data).__name__
                        if kind not in ('Sum', 'Gauge', 'Histogram'):
                            error = 'unsupported_metric_type'
                            continue
                        dimensions = getattr(spec, 'allowed_dimensions', ())
                        points = []
                        source_points = list(data.data_points)
                        overflow_points = [p for p in source_points if (p.attributes or {}).get('otel.metrics.overflow') is True]
                        source_points = overflow_points + [p for p in source_points if (p.attributes or {}).get('otel.metrics.overflow') is not True]
                        for point in source_points:
                            if len(points) >= 100:
                                truncated = True
                                truncation_scope.append(metric.name + ':points')
                                break
                            attrs = dict(point.attributes or {})
                            safe = self._project(attrs, dimensions) if self._project else safe_dimensions(attrs, dimensions)
                            if attrs.get('otel.metrics.overflow') is True:
                                safe['nkb.metric.overflow'] = True
                            projected = dict(attributes=safe,
                                             overflow=attrs.get('otel.metrics.overflow') is True,
                                             aggregation_temporality=getattr(getattr(data, 'aggregation_temporality', None), 'name', None),
                                             start_time=_utc(point.start_time_unix_nano) if point.start_time_unix_nano else None,
                                             end_time=_utc(point.time_unix_nano))
                            if kind == 'Histogram':
                                projected.update({key:_number(getattr(point,key,None)) for key in ('count','sum','min','max')})
                            else:
                                projected['value'] = _number(point.value)
                            points.append(projected)
                        if metric.name in SYSTEM_INSTRUMENTS:
                            groups = {}
                            for point in points:
                                key = json.dumps(point['attributes'], sort_keys=True)
                                groups.setdefault(key, []).append(point)
                            points = []
                            for group in groups.values():
                                point = dict(group[0])
                                values = [p['value'] for p in group if p.get('value') is not None]
                                if values:
                                    point['value'] = sum(values) / len(values) if metric.name == 'system.cpu.utilization' else sum(values)
                                point['series_count'] = len(group)
                                points.append(point)
                        instruments.append(dict(name=metric.name, kind=getattr(spec, 'kind', kind.lower()),
                                                unit=getattr(spec, 'unit', metric.unit), points=points, window='provider_lifetime' if getattr(getattr(data, 'aggregation_temporality', None), 'name', None) == 'CUMULATIVE' else ('collection_interval' if getattr(getattr(data, 'aggregation_temporality', None), 'name', None) == 'DELTA' else 'instantaneous')))
            json.dumps(instruments, allow_nan=False)
        except Exception:
            with self._lock:
                if self._closed:
                    return
                self._state.update(status='degraded', error='metric_collection_failed', stale=True)
                self._state['collection_count'] += 1
            return
        now = _utc()
        with self._lock:
            if self._closed:
                return
            self._state.update(status='degraded' if error else 'healthy', collected_at=now,
                               error=error, truncated=truncated, truncation_scope=sorted(set(truncation_scope)), last_success_at=now,
                               stale=False, instruments=instruments)
            self._state['collection_count'] += 1

    def latest(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def clear(self):
        with self._lock:
            self._closed = True
            self._state.update(status='disabled', instruments=[], error=None, stale=False)

    def degrade(self, error):
        with self._lock:
            self._state.update(status='degraded', error=error, stale=True)


class SafeLogBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = deque(maxlen=100)
        self._evicted = 0
        self._closed = False

    def append(self, item):
        projected = {key:copy.deepcopy(item.get(key)) for key in
                     ('timestamp','severity','event','trace_id','span_id','attributes')}
        projected['attributes'] = project_log_attributes(projected.get('attributes') or {})
        json.dumps(projected, allow_nan=False)
        with self._lock:
            if self._closed:
                return
            if len(self._items) == 100:
                self._evicted += 1
            self._items.append(projected)

    def latest(self):
        with self._lock:
            items = list(reversed(self._items))[:10]
            count, evicted = len(self._items), self._evicted
        return dict(status='disabled' if self._closed else 'healthy', items=copy.deepcopy(items), retained_count=count,
                    capacity=100, returned_count=len(items), evicted_count=evicted)

    def clear(self):
        with self._lock:
            self._closed = True
            self._items.clear()
            self._evicted = 0
