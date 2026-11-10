"""Exact shared log projection and opt-in isolated Collector execution."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import pytest
import yaml

from app.application.telemetry_contract import ATTRIBUTES, CONTENT_ATTRIBUTES, CONTENT_FIELDS, CONTENT_METADATA
from app.infrastructure.observability.instrumentors import ALLOWED_AUTO_ATTRIBUTES

STACK = Path(__file__).resolve().parents[2] / 'n-agent/docker/observability'
CONFIG = STACK / 'otel-collector.yaml'
# 共享栈基线快照：n-agent 仓 otel-collector.yaml 在成本观测行（T5：nagent/
# nkb 的 model.cost_*/billing_*/price_id/pricing_version）合入后的已知良好
# 状态原样复制。基线不再钉在 git HEAD——n-agent 侧后续任何无关演进都会使
# 本锁定失败，须有意识地重新生成快照（对照 n-agent 仓当前文件复制）。
BASELINE = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'otel-collector-nagent-baseline.yaml'
CONDITION = 'resource.attributes["service.name"] == "n-kb"'
EXECUTION = (set(ATTRIBUTES) - CONTENT_ATTRIBUTES) | set(ALLOWED_AUTO_ATTRIBUTES)


def statements(config):
    return yaml.safe_load(config)['processors']['transform/loki_json']['log_statements'][0]['statements']


def test_exact_contract_projection_and_nagent_unchanged():
    current = CONFIG.read_text()
    nkb = [s for s in statements(current) if CONDITION in s]
    mapped = {m.group(1) for s in nkb if (m := re.match(r'set\(cache\["execution"\]\["([^"]+)"\]', s))}
    assert mapped == EXECUTION
    for field in CONTENT_FIELDS:
        assert any(f'attributes["nkb.content.{field}"]' in s for s in nkb)
        for suffix in CONTENT_METADATA:
            assert any(f'attributes["nkb.content.{field}_{suffix}"]' in s for s in nkb)
    assert 'set(attributes, {}) where ' + CONDITION in nkb
    baseline = BASELINE.read_text()
    assert [s for s in statements(current) if CONDITION not in s] == [s for s in statements(baseline) if CONDITION not in s]
    assert not any('nkb.details.' in s or 'attributes["outcome"]' in s for s in nkb)


def value(v):
    if isinstance(v, dict):
        return {'kvlistValue': {'values': [{'key':k, 'value':value(x)} for k,x in v.items()]}}
    if type(v) is bool:
        return {'boolValue':v}
    if type(v) is int:
        return {'intValue':str(v)}
    if type(v) is float:
        return {'doubleValue':v}
    return {'stringValue':v}


def decode(v):
    if 'kvlistValue' in v:
        return {x['key']:decode(x['value']) for x in v['kvlistValue'].get('values', [])}
    if 'arrayValue' in v:
        return [decode(x) for x in v['arrayValue'].get('values', [])]
    if 'intValue' in v:
        return int(v['intValue'])
    return next(iter(v.values())) if v else None


def fixture():
    facts = {'nkb.index.operation':'run', 'nkb.index.outcome':'success', 'nkb.index.summary':'index.run completed', 'nkb.index.duration_ms':12.5, 'nkb.index.document_id':'doc-1', 'nkb.index.job_id':'job-1', 'nkb.index.chunk_count':0, 'nkb.request.channel':'api'}
    details = {field: {'content':f'safe {field}', 'original_bytes':64, 'observed_bytes':32, 'truncated':True, 'incomplete':True} for field in CONTENT_FIELDS}
    contents = {f'nkb.content.{field}' + ('' if k == 'content' else '_'+k):v for field,envelope in details.items() for k,v in envelope.items()}
    def record(event, attrs, message):
        return {'timeUnixNano':'1700000000000000000','observedTimeUnixNano':'1700000000000000000','severityNumber':9,'severityText':'INFO','body':value(message),'traceId':'a'*32,'spanId':'b'*16,'attributes':[{'key':k,'value':value(v)} for k,v in dict(attrs,event=event).items()]}
    rogue = {'unknown.secret':'must-not-leak', 'nkb.outcome':'error', 'outcome':'error', 'nagent.agent.run_id':'must-not-leak'}
    records = [record('nkb.index.completed', facts | contents | rogue, 'index.run completed'), record('library_log', facts | contents | rogue, 'safe library message')]
    agent = record('nagent.agent.completed', {'nagent.agent.run_id':'run-1','nagent.agent.outcome':'success','nagent.content.http_request':'safe request','nagent.content.http_request_original_bytes':12,'unknown.secret':'agent unchanged'}, 'agent completed')
    def resource(service, logs):
        return {'resource':{'attributes':[{'key':'service.name','value':value(service)}]},'scopeLogs':[{'scope':{'name':'fixed.test'},'logRecords':logs}]}
    return {'resourceLogs':[resource('n-kb',records),resource('n-agent',[agent])]}, facts, details


def convert(config, payload):
    """Only IO is replaced; processors and their ordering come from real config."""
    original = yaml.safe_load(config)
    pipeline = original['service']['pipelines']['logs']
    isolated = {'receivers':{'otlp':{'protocols':{'http':{'endpoint':'0.0.0.0:4318'}}}}, 'processors':{k:original['processors'][k] for k in pipeline['processors']},'exporters':{'file':{'path':'/out/logs.json','flush_interval':'100ms'}},'service':{'telemetry':{'metrics':{'level':'none'}},'pipelines':{'logs':{'receivers':['otlp'],'processors':pipeline['processors'],'exporters':['file']}}}}
    name = 'nkb-collector-test-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='nkb-collector-') as directory:
        path = Path(directory)
        (path/'config.yaml').write_text(yaml.safe_dump(isolated))
        try:
            subprocess.run(['docker','run','-d','--name',name,'-p','127.0.0.1::4318','-v',f'{path}:/out','otel/opentelemetry-collector-contrib:0.137.0','--config=/out/config.yaml'],check=True,capture_output=True,timeout=15)
            port = subprocess.check_output(['docker','port',name,'4318/tcp'],text=True, timeout=15).strip().rsplit(':',1)[1]
            deadline = time.monotonic()+25
            while True:
                try:
                    req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/logs',json.dumps(payload).encode(),{'Content-Type':'application/json'})
                    with urllib.request.urlopen(req,timeout=2) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if time.monotonic()>deadline:
                        pytest.fail(subprocess.check_output(['docker','logs',name],stderr=subprocess.STDOUT,text=True, timeout=15))
                    time.sleep(.1)
            while time.monotonic()<deadline:
                output=path/'logs.json'
                if output.exists() and output.stat().st_size:
                    result={}
                    for line in output.read_text().splitlines():
                        for resource in json.loads(line)['resourceLogs']:
                            service=next(decode(x['value']) for x in resource['resource']['attributes'] if x['key']=='service.name')
                            result.setdefault(service,[]).extend(record for scope in resource['scopeLogs'] for record in scope['logRecords'])
                    if sum(map(len,result.values()))==3:
                        return result
                time.sleep(.1)
            pytest.fail('Collector did not export all fixed records before deadline')
        finally:
            # run may time out after Docker created the named container. Always
            # attempt removal, then independently verify absence with a bound.
            primary = sys.exception()
            cleanup_errors = []
            try:
                subprocess.run(['docker','rm','-f',name],check=False,capture_output=True,timeout=15)
            except (subprocess.SubprocessError, OSError) as exc:
                cleanup_errors.append(str(exc))
            try:
                remaining = subprocess.check_output(['docker','ps','-a','--filter',f'name={name}','--format','{{.Names}}'],text=True,timeout=15)
                if name in remaining:
                    cleanup_errors.append(f'container still present: {name}')
            except (subprocess.SubprocessError, OSError) as exc:
                cleanup_errors.append(f'cannot verify removal of {name}: {exc}')
            if cleanup_errors:
                diagnostic = 'Collector cleanup: ' + '; '.join(cleanup_errors)
                if primary is not None:
                    primary.add_note(diagnostic)
                else:
                    raise RuntimeError(diagnostic)



@pytest.mark.skipif(os.getenv('NKB_COLLECTOR_SMOKE') != '1', reason='set NKB_COLLECTOR_SMOKE=1 for real isolated Collector conversion')
def test_real_collector_conversion_and_nagent_equivalence():
    subprocess.run(['docker','image','inspect','otel/opentelemetry-collector-contrib:0.137.0'],check=True,capture_output=True,timeout=15)
    payload, facts, details = fixture()
    # 与基线锁定同一快照：n-agent 分支对照已知良好状态，而非 git HEAD
    baseline_config = BASELINE.read_text()
    baseline = convert(baseline_config, payload)
    actual = convert(CONFIG.read_text(), payload)
    def normalize(record):
        return {key: decode(val) if key == 'body' else val for key, val in record.items()}
    assert [normalize(r) for r in actual['n-agent']] == [normalize(r) for r in baseline['n-agent']]
    for record, message, event in zip(actual['n-kb'], ('index.run completed','safe library message'), ('nkb.index.completed','library_log')):
        body=decode(record['body'])
        assert body['context']['execution'] == facts
        assert body['details'] == details
        assert body['event'] == event and body['severity']=='INFO' and body['message']==message
        assert body['trace_id']=='a'*32 and body['span_id']=='b'*16
        assert record.get('attributes',[]) == []
        assert 'must-not-leak' not in json.dumps(record)


def test_launch_timeout_still_attempts_bounded_cleanup(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == 'run':
            raise subprocess.TimeoutExpired(command, 15)
        return subprocess.CompletedProcess(command, 0)

    def output(command, **kwargs):
        calls.append((command, kwargs))
        return ''

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(subprocess, 'check_output', output)
    with pytest.raises(subprocess.TimeoutExpired):
        convert(CONFIG.read_text(), fixture()[0])
    assert [command[1] for command, _ in calls] == ['run', 'rm', 'ps']
    assert all(0 < kwargs['timeout'] <= 15 for _, kwargs in calls)


def test_cleanup_timeout_preserves_launch_failure_and_checks_remaining(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command[1])
        assert 0 < kwargs['timeout'] <= 15
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    def output(command, **kwargs):
        calls.append(command[1])
        assert 0 < kwargs['timeout'] <= 15
        return ''

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(subprocess, 'check_output', output)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        convert(CONFIG.read_text(), fixture()[0])
    assert failure.value.cmd[1] == 'run'
    assert calls == ['run', 'rm', 'ps']
    assert any('Collector cleanup' in note and 'timed out' in note for note in failure.value.__notes__)
