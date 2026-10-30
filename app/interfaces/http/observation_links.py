"""Grafana browser links; same Explore pane contract and UIDs as N-Agent."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TEMPO_UID = 'nagent-tempo'


def valid_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None:
            return None
        if any(re.search(r'token|password|secret|api.?key|credential', key, re.I) for key, _ in parse_qsl(url.query)):
            return None
        if url.port is not None and not 0 < url.port <= 65535:
            return None
        return url
    except ValueError:
        return None


def _url(base, params=None, path=None):
    parsed = valid_url(base)
    if parsed is None:
        return None
    updates = params or {}
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in updates]
    query.extend(updates.items())
    return urlunsplit((parsed.scheme, parsed.netloc, path if path is not None else parsed.path, urlencode(query), ''))


def _derived(base, suffix):
    parsed = valid_url(base)
    if parsed is None:
        return None
    # base_url may be the old dashboard entry rather than a Grafana root.
    root = parsed.path.split('/d/', 1)[0].removesuffix('/explore').rstrip('/')
    return _url(base, path=root + suffix)


def _trace_base(settings):
    return settings.otel_grafana_traces_base_url or _derived(settings.otel_grafana_base_url, '/explore')


def _explore(base, query, query_type='traceql'):
    parsed = valid_url(base)
    if parsed is None or not parsed.path.rstrip('/').endswith('/explore'):
        return None
    pane = {'trace': {'datasource': TEMPO_UID,
                      'queries': [{'refId': 'A', 'queryType': query_type, 'query': query,
                                   'datasource': {'type': 'tempo', 'uid': TEMPO_UID}}],
                      'range': {'from': 'now-1h', 'to': 'now'}}}
    return _url(base, {'schemaVersion': '1', 'panes': json.dumps(pane, ensure_ascii=False)})


def trace_link(settings, trace_id, instance_id=None):
    if not isinstance(trace_id, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', trace_id) or int(trace_id, 16) == 0:
        return None
    return _explore(_trace_base(settings), trace_id.lower(), 'traceId')


def build_links(settings, instance_id=None):
    params = {'var-service': 'n-kb'}
    if instance_id:
        params['var-instance'] = str(instance_id)
    links = {}
    for name, field, suffix in (
        ('Metric', 'otel_grafana_metrics_base_url', '/d/nkb-overview'),
        ('Log', 'otel_grafana_logs_base_url', '/d/nkb-logs'),
        ('Profile', 'otel_grafana_profile_base_url', '/d/nkb-profile'),
    ):
        base = getattr(settings, field, '') or _derived(settings.otel_grafana_base_url, suffix)
        # Profile is service-level; no nonexistent instance selector.
        value = _url(base, {'var-service': 'n-kb'} if name == 'Profile' else params)
        if value:
            links[name] = value
    trace_base = _trace_base(settings)
    selectors = ['resource.service.name=' + json.dumps('n-kb')]
    if instance_id:
        selectors.append('resource.service.instance.id=' + json.dumps(str(instance_id)))
    value = _explore(trace_base, '{' + ' && '.join(selectors) + '}') or _url(trace_base, params)
    if value:
        links['Trace'] = value
    return links
