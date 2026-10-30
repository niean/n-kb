"""Small business summaries; all content still uses the injected detail policy."""
from pathlib import PurePath
from urllib.parse import urlsplit

from . import telemetry_details as details


def safe_source(source):
    source = source if type(source) is dict else {}
    kind = source.get('kind')
    result = {'kind':kind if kind in ('upload','file','web','url','http','https') else 'other'}
    uri = source.get('uri')
    if type(uri) is not str:
        return result
    try:
        parsed = urlsplit(uri)
        if parsed.scheme in ('http','https') and parsed.hostname:
            result.update(scheme=parsed.scheme,host=parsed.hostname)
            if parsed.port is not None:
                result['port'] = parsed.port
        elif not parsed.scheme and not uri.startswith(('/','\\')):
            result['filename'] = PurePath(uri).name
    except (ValueError,TypeError):
        pass
    return result


def retrieval_output(results):
    kept = [dict(document_id=item.document_id,chunk_id=item.chunk_id,score=item.score,
                 snippet=item.snippet,source=safe_source(item.source)) for item in results[:10]]
    payload = dict(result_count=len(results),returned_count=len(kept),results_truncated=len(results)>len(kept),results=kept)
    # Preserve parseable output and an honest retained count after byte limits.
    while kept:
        envelope = details.project_details(retrieval_output=payload).get('retrieval_output')
        if not envelope or not envelope.get('truncated'):
            break
        kept.pop()
        payload.update(returned_count=len(kept),results_truncated=True)
    return payload


def embedding_input(model,inputs):
    prefixes = []
    truncated = len(inputs)>3
    for text in inputs[:3]:
        envelope = details.project_details(embedding_input=text).get('embedding_input',{})
        safe = envelope.get('content','[details unavailable]')
        raw = safe.encode('utf-8')
        truncated = truncated or envelope.get('truncated',False) or len(raw)>256
        prefixes.append(raw[:256].decode('utf-8',errors='ignore'))
    return dict(model=model,input_count=len(inputs),text_prefixes=prefixes,prefixes_truncated=truncated)
