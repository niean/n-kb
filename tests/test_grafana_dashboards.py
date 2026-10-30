"""Shared-stack provisioning and cross-signal contracts; no live backends."""
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT.parent / 'n-agent' / 'docker' / 'observability'
DASHBOARDS = STACK / 'grafana' / 'provisioning' / 'dashboards'


@pytest.fixture
def dashboards():
    if not STACK.is_dir():
        pytest.skip('adjacent n-agent checkout required for shared dashboard validation')
    return {name: json.loads((DASHBOARDS / f'nkb-{name}.json').read_text()) for name in ('overview', 'logs', 'profile')}


def test_provisioning_references_and_all_uids_are_unique(dashboards):
    provider = yaml.safe_load((DASHBOARDS / 'provider.yaml').read_text())
    assert provider['providers'][0]['options']['path'] == '/etc/grafana/provisioning/dashboards'
    compose = yaml.safe_load((STACK / 'docker-compose.yml').read_text())
    assert './grafana/provisioning:/etc/grafana/provisioning:ro' in compose['services']['grafana']['volumes']
    all_docs = [json.loads(p.read_text()) for p in DASHBOARDS.glob('*.json')]
    assert len({d['uid'] for d in all_docs}) == len(all_docs)
    datasources = yaml.safe_load((STACK / 'grafana/provisioning/datasources/datasources.yaml').read_text())['datasources']
    refs = {d.get('uid', d['name']) for d in datasources} | {d['name'] for d in datasources}
    for key, dashboard in dashboards.items():
        assert dashboard['uid'] == f'nkb-{key}'
        assert 'n-kb' in dashboard['tags']
        for panel in dashboard['panels']:
            source = panel.get('datasource')
            if source:
                assert (source.get('uid') if isinstance(source, dict) else source) in refs
    tempo = next(d for d in datasources if d['type'] == 'tempo')
    assert tempo['jsonData']['tracesToLogsV2']['filterByTraceID'] is True
    loki = next(d for d in datasources if d['type'] == 'loki')
    assert loki['jsonData']['derivedFields'][0]['datasourceUid'] == 'nagent-tempo'


def test_queries_cover_actual_registry_and_filter_contract(dashboards):
    from app.application.telemetry_semantics import INSTRUMENTS
    overview = dashboards['overview']
    queries = '\n'.join(t.get('expr', '') for p in overview['panels'] for t in p.get('targets', []))
    for name, spec in INSTRUMENTS.items():
        base = name.replace('.', '_')
        if spec.unit == 's':
            base += '_seconds'
        assert base in queries, f'missing panel for {name}'
    assert 'histogram_quantile(0.95, sum by (le' in queries
    assert 'le="0"' in queries
    assert 'gen_ai_client_token_usage_sum' in queries
    assert 'or vector(0)' not in queries
    assert 'service_name=~"$service"' in queries
    assert 'service_instance_id=~' not in queries
    assert '${instance' not in queries
    assert 'process_memory_usage_bytes' in queries
    variables = overview['templating']['list']
    assert len(variables) == 1
    assert variables[0]['name'] == 'service'
    assert not variables[0]['multi'] and variables[0]['includeAll']
    profile = dashboards['profile']['panels'][0]['targets'][0]
    assert profile['labelSelector'] == '{service_name="n-kb"}'
    assert 'instance' not in json.dumps(profile)
    assert profile['profileTypeId'] == 'process_cpu:cpu:nanoseconds:cpu:nanoseconds'
    assert dashboards['profile']['templating']['list'] == []
    logs = '\n'.join(t.get('expr', '') for p in dashboards['logs']['panels'] for t in p.get('targets', []))
    assert 'service_instance_id=~' not in logs
    assert '${instance' not in logs
    assert 'service_name=~"$service"' in logs


def test_collector_only_projects_approved_nkb_facts():
    collector = yaml.safe_load((STACK / 'otel-collector.yaml').read_text())
    statements = collector['processors']['transform/loki_json']['log_statements'][0]['statements']
    nkb = [s for s in statements if 'resource.attributes["service.name"] == "n-kb"' in s]
    assert nkb
    for field in ('outcome', 'duration_ms', 'result_count', 'chunk_count', 'stage'):
        assert any(f'attributes["{field}"]' in s for s in nkb)
    assert all('query' not in s and 'body' not in s and 'MergeMaps' not in s for s in nkb)
    assert collector['service']['pipelines']['logs']['exporters'] == ['otlphttp/loki', 'debug']


def test_dashboard_statistical_contracts_and_mapping(dashboards):
    from app.application.telemetry_semantics import INSTRUMENTS
    mapping = (STACK / 'nkb-metric-name-mapping.md').read_text()
    for name, spec in INSTRUMENTS.items():
        row = next(line for line in mapping.splitlines() if line.startswith(f'| {name} |'))
        assert spec.kind in row and spec.unit in row
        for boundary in spec.boundaries:
            assert str(boundary) in row
    assert '| nagent.' not in mapping and '| gen_ai.client.inference.' not in mapping
    panels = dashboards['overview']['panels']
    for panel in panels:
        expression = panel['targets'][0]['expr']
        if '错误率' in panel['title']:
            numerator, denominator = expression.split(' / ', 1)
            assert 'outcome="error"' in numerator
            assert 'outcome=~"success|error"' in denominator
            assert 'cancelled' not in expression
            assert denominator.endswith(' > 0)')
        if 'p95' in panel['title']:
            assert expression.startswith('histogram_quantile(0.95, sum by (le')
            assert '(rate(' in expression and '_bucket{' in expression
        if '吞吐' in panel['title']:
            assert expression.startswith('sum by (outcome) (rate(')
    empty = next(p for p in panels if '空命中' in p['title'])['targets'][0]['expr']
    numerator, denominator = empty.split(' / ', 1)
    assert 'nkb_retrieval_results_bucket{' in numerator and 'le="0"' in numerator
    assert 'nkb_retrieval_results_count{' in denominator and denominator.endswith(' > 0)')
    token = next(p for p in panels if 'Token' in p['title'])['targets'][0]['expr']
    assert 'rate(gen_ai_client_token_usage_sum{' in token and '_count' not in token


def test_dashboard_layout_components_and_filters_match_nagent(dashboards):
    reference_overview = json.loads((DASHBOARDS / 'nagent-overview.json').read_text())
    overview = dashboards['overview']
    assert [p['gridPos'] for p in overview['panels'][:11]] == [p['gridPos'] for p in reference_overview['panels']]
    assert all(p['type'] == 'stat' for p in overview['panels'] if '吞吐' in p['title'])
    assert all(p['type'] == 'timeseries' for p in overview['panels'] if '吞吐' not in p['title'])
    reference_variable = reference_overview['templating']['list'][0]
    variable = overview['templating']['list'][0]
    for key in ('name', 'label', 'type', 'datasource', 'includeAll', 'multi', 'current'):
        assert variable[key] == reference_variable[key]

    reference_logs = json.loads((DASHBOARDS / 'nagent-logs.json').read_text())
    logs = dashboards['logs']
    assert [p['type'] for p in logs['panels']] == [p['type'] for p in reference_logs['panels']]
    assert [p['title'] for p in logs['panels']] == [p['title'] for p in reference_logs['panels']]
    assert [p['gridPos'] for p in logs['panels']] == [p['gridPos'] for p in reference_logs['panels']]
    assert logs['templating']['list'] == reference_logs['templating']['list']

    reference_profile = json.loads((DASHBOARDS / 'nagent-profile.json').read_text())
    profile = dashboards['profile']
    assert profile['panels'][0]['gridPos'] == reference_profile['panels'][0]['gridPos']
    assert profile['panels'][0]['type'] == reference_profile['panels'][0]['type']
    assert profile['templating']['list'] == []


def test_signal_queries_match_existing_nagent_contract(dashboards):
    datasources = yaml.safe_load((STACK / 'grafana/provisioning/datasources/datasources.yaml').read_text())['datasources']
    tempo = next(d for d in datasources if d['type'] == 'tempo')['jsonData']['tracesToLogsV2']
    assert {'key':'service.name', 'value':'service_name'} in tempo['tags']
    assert tempo['spanStartTimeShift'] == '-5s' and tempo['spanEndTimeShift'] == '5s'
    derived = next(d for d in datasources if d['type'] == 'loki')['jsonData']['derivedFields'][0]
    import re
    assert re.search(derived['matcherRegex'], '{"trace_id":"'+'a'*32+'"}').group(1) == 'a'*32
    assert not re.search(derived['matcherRegex'], '{"query":"private"}')
    reference = json.loads((DASHBOARDS / 'nagent-profile.json').read_text())['panels'][0]['targets'][0]
    target = dashboards['profile']['panels'][0]['targets'][0]
    for key in ('queryType', 'profileTypeId', 'datasource', 'maxNodes'):
        assert target[key] == reference[key]
    for panel in dashboards['logs']['panels']:
        target = panel['targets'][0]
        assert target['editorMode'] == 'code' and target['queryType'] == 'range'
        assert target['datasource']['uid'] == derived.get('uid', 'P8E80F9AEF21F6940')
    collector = yaml.safe_load((STACK / 'otel-collector.yaml').read_text())
    statements = collector['processors']['transform/loki_json']['log_statements'][0]['statements']
    assert any('"trace_id": trace_id.string' in s for s in statements)
    assert not any('attributes["query"]' in s or 'attributes["content"]' in s for s in statements if '== "n-kb"' in s)
