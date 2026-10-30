import json
from urllib.parse import urlsplit, parse_qs
from app.config import Settings


def test_four_signal_links_and_precise_trace_preserve_subpath():
    from app.interfaces.http.observation_links import build_links, trace_link
    settings = Settings(otel_grafana_base_url='https://grafana.example/obs/')
    links = build_links(settings, 'instance-1')
    assert '/obs/d/nkb-overview' in links['Metric']
    assert '/obs/d/nkb-logs' in links['Log']
    assert '/obs/d/nkb-profile' in links['Profile']
    href = trace_link(settings, 'a' * 32, 'instance-1')
    parsed = urlsplit(href)
    assert parsed.path == '/obs/explore'
    panes = json.loads(parse_qs(parsed.query)['panes'][0])
    assert panes['trace']['queries'][0]['query'] == 'a' * 32
    assert trace_link(settings, '0' * 32, 'instance-1') is None


def test_legacy_trace_link_is_not_rewritten_and_bad_urls_hidden():
    from app.interfaces.http.observation_links import build_links, trace_link
    settings = Settings(otel_grafana_traces_base_url='https://custom.example/traces')
    assert build_links(settings, 'i')['Trace'].startswith('https://custom.example/traces')
    assert trace_link(settings, 'a' * 32, 'i') is None


def test_repeated_query_and_fragment_are_preserved_or_removed_safely():
    from app.interfaces.http.observation_links import _url, valid_url
    href = _url('https://grafana.example/obs/explore?var-region=a&var-region=b&orgId=2#frag', {'schemaVersion':'1'})
    parsed = urlsplit(href)
    assert parse_qs(parsed.query)['var-region'] == ['a', 'b']
    assert parsed.fragment == ''
    for url in ['javascript:alert(1)', 'https://u:p@g.example/', 'https://g.example:99999/', 'https://g.example/?token=private', 'https://g.example/\n']:
        assert valid_url(url) is None
