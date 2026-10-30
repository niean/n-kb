"""N-Agent style HTTP byte taps; never read ahead or retain file/vector bodies."""
from __future__ import annotations

import json
import zlib
from urllib.parse import parse_qs

import httpx

from . import logging as logs


def safe_headers(headers):
    result = {}
    for name, value in headers.items():
        name = name.lower()
        if name in ('authorization', 'cookie', 'set-cookie'):
            result[name] = '[REDACTED]'
        elif name in ('content-type', 'content-encoding'):
            result[name] = value[:256]
        elif name == 'content-length' and value.isascii() and value.isdigit() and len(value) <= 20:
            result[name] = value
    return result


class Body:
    def __init__(self, headers=None, *, enabled=True, decoded=False):
        self.headers = safe_headers(dict(headers or {}))
        self.limit = 4 * logs.content_max_bytes()
        self.data = bytearray()
        self.observed = 0
        self.complete = False
        self.overflow = False
        self.enabled = enabled and not self.headers.get('content-type', '').lower().startswith('multipart/')
        self.layer = 'decoded' if decoded else 'transport'
        self.decoder = None
        encoding = self.headers.get('content-encoding', '').lower()
        if encoding and not decoded:
            if encoding in ('gzip', 'deflate'):
                self.decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
            elif encoding != 'identity':
                self.enabled = False
        self.input_bytes = 0
        self.chunks = 0

    def append(self, chunk):
        self.observed += len(chunk)
        if not self.enabled or self.overflow:
            return
        self.chunks += 1
        available = self.limit - self.input_bytes
        value = chunk[:max(0, available)]
        self.input_bytes += len(value)
        self.overflow |= len(chunk) > available or self.chunks > 4096
        try:
            remaining = self.limit - len(self.data)
            decoded = self.decoder.decompress(value, remaining + 1) if self.decoder else value
            self.data.extend(decoded[:remaining])
            self.overflow |= len(decoded) > remaining or bool(self.decoder and self.decoder.unconsumed_tail)
        except zlib.error:
            self.overflow = True
            self.data.clear()

    def observe(self, chunk):
        try:
            self.append(chunk)
        except Exception:
            self.overflow = True
            self.data.clear()

    def value(self):
        if not self.enabled:
            return {'body_omitted':'metadata_only'}
        if not self.complete or self.overflow or (self.decoder and not self.decoder.eof):
            return {'body_omitted':'incomplete_or_truncated'}
        content_type = self.headers.get('content-type', '').lower()
        if content_type.startswith(('image/', 'audio/', 'video/', 'application/octet-stream')):
            return {'body_omitted':'binary'}
        try:
            text = self.data.decode('utf-8')
            if 'application/x-www-form-urlencoded' in content_type:
                return parse_qs(text, keep_blank_values=True, max_num_fields=128)
            if 'json' in content_type or text.lstrip().startswith(('{', '[')):
                return json.loads(text)
            return text
        except (ValueError, UnicodeError, RecursionError):
            return {'body_omitted':'invalid_encoding_or_json'}


def _content_policy(value, path, response):
    """Select RAG protocol fields; credential/content rules remain injected."""
    if type(value) is not dict:
        return value
    if path == '/api/embed':
        if response:
            vectors = value.get('embeddings', [])
            if type(vectors) is not list:
                vectors = []
            return {'output_count':len(vectors), 'dimensions':sorted({len(v) for v in vectors[:128] if type(v) is list}),
                    'input_tokens':value.get('prompt_eval_count')}
        inputs = value.get('input', [])
        inputs = [inputs] if type(inputs) is str else inputs if type(inputs) is list else []
        prefixes = []
        for text in inputs[:3]:
            if type(text) is not str:
                continue
            # Only sanitize fully observed bounded text; never cut a credential
            # before sanitizing it. Long input conservatively becomes metadata.
            safe = logs._sanitize_content(text) if len(text) <= logs.content_max_bytes() else None
            prefixes.append(safe.encode('utf-8')[:256].decode('utf-8', errors='ignore') if safe else '[prefix omitted]')
        return {'model':value.get('model'), 'input_count':len(inputs), 'text_prefixes':prefixes}
    if path.startswith('/collections/'):
        if not response:
            result = {k:value[k] for k in ('limit','score_threshold','filter','with_payload') if k in value}
            vector = value.get('query', value.get('vector'))
            if type(vector) is list:
                result['vector_dimensions'] = len(vector)
            if 'points' in value:
                result['point_count'] = len(value['points']) if type(value['points']) is list else None
            return result
        points = value.get('result', [])
        if type(points) is dict:
            points = points.get('points', [])
        if type(points) is list:
            return {'result_count':len(points), 'results':[{
                'id':p.get('id'), 'score':p.get('score'),
                'snippet':p.get('payload', {}).get('text') if type(p.get('payload')) is dict else None,
            } for p in points[:10] if type(p) is dict], 'results_truncated':len(points)>10}
        return {k:value[k] for k in ('status','time') if k in value}
    if path == '/documents' and response:
        return {k:value[k] for k in ('id','filename','status','size_bytes') if k in value}
    results = value.get('results')
    if type(results) is list:
        return {'query':value.get('query'), 'result_count':len(results), 'results_truncated':len(results)>10,
                'results':[{k:item[k] for k in ('document_id','chunk_id','score','snippet') if k in item}
                           for item in results[:10] if type(item) is dict]}
    return value


class Exchange:
    def __init__(self, *, request=None, scope=None):
        if request is not None:
            self.path = request.url.path
            self.method = request.method
            headers = request.headers
            raw_query = request.url.query
        else:
            self.path = scope.get('path', '')
            self.method = scope.get('method', '')
            headers = {k.decode('latin1').lower():v.decode('latin1') for k,v in scope.get('headers', [])}
            raw_query = scope.get('query_string', b'')
        body_allowed = self.path in ('/retrieval/search','/api/embed') or self.path.startswith('/collections/')
        self.request = Body(headers, enabled=body_allowed)
        self.response = Body(enabled=body_allowed or self.path == '/documents')
        self.response_allowed = body_allowed or self.path == '/documents'
        self.query = {}
        if len(raw_query) <= 2048:
            try:
                parsed = parse_qs(raw_query.decode('utf-8'), max_num_fields=32)
                self.query = {k:v[:8] for k,v in parsed.items() if k in ('top_k','min_score','limit','offset','wait')}
            except (ValueError, UnicodeError):
                pass
        if request is not None:
            try:
                self.request.observe(request.content)
                self.request.complete = True
            except httpx.RequestNotRead:
                pass

    def response_headers(self, headers, *, decoded=False):
        try:
            self.response = Body(headers, enabled=self.response_allowed, decoded=decoded)
        except Exception:
            self.response.enabled = False
            self.response.overflow = True

    def project(self):
        fields = {}
        for name, body in (('http_request', self.request), ('http_response', self.response)):
            fields[name] = {'method':self.method, 'headers':body.headers,
                'body':_content_policy(body.value(), self.path, name == 'http_response'),
                'observed_layer':body.layer}
            if name == 'http_request':
                fields[name]['query'] = self.query
        result = logs._call('project', **fields)
        for name, body in (('http_request', self.request), ('http_response', self.response)):
            if name in result:
                envelope = result[name]
                envelope['observed_bytes'] = body.observed
                if not body.complete or body.overflow:
                    envelope.update(original_bytes=None, truncated=True, incomplete=not body.complete)
                # Metadata has changed: validation rechecks content and budget.
            body.data.clear()
            body.headers.clear()
        self.query.clear()
        return logs._call('validate', result)


class RequestStream(httpx.SyncByteStream):
    def __init__(self, stream, body):
        self.stream, self.body = stream, body

    def __iter__(self):
        for chunk in self.stream:
            self.body.observe(chunk)
            yield chunk
        self.body.complete = True

    def close(self):
        self.stream.close()


class AsyncRequestStream(httpx.AsyncByteStream):
    def __init__(self, stream, body):
        self.stream, self.body = stream, body

    async def __aiter__(self):
        async for chunk in self.stream:
            self.body.observe(chunk)
            yield chunk
        self.body.complete = True

    async def aclose(self):
        await self.stream.aclose()
