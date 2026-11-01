"""Fixed response-shape metadata; never export provider messages or tokens."""
from __future__ import annotations

import inspect


def safe_reply_metadata(value):
    """Describe known acknowledgement fields without retaining arbitrary text."""
    result = {'body_kind': ('object' if isinstance(value, dict) else
        'array' if isinstance(value, list) else 'boolean' if isinstance(value, bool) else
        'null' if value is None else 'number' if isinstance(value, (int, float)) else 'string'),
        'nodes': []}
    queue = [('root', value, 0)]
    known_statuses = {'ok', 'success', 'succeeded', 'complete', 'completed',
        'pending', 'queued', 'accepted', 'rejected', 'error', 'failed', 'failure'}
    while queue:
        path, node, depth = queue.pop(0)
        if not isinstance(node, dict):
            continue
        summary = {'path': path}
        if isinstance(node.get('result'), bool):
            summary['result'] = node['result']
        if type(node.get('code')) is int and 0 <= node['code'] <= 599:
            summary['code'] = node['code']
        status = node.get('status')
        if isinstance(status, str) and status.lower() in known_statuses:
            summary['status'] = status.lower()
        for field in ('error', 'reason', 'message'):
            if field in node:
                summary[field + '_present'] = node[field] is not None and node[field] != ''
        result['nodes'].append(summary)
        if depth < 2:
            for field in ('response', 'result', 'data'):
                if isinstance(node.get(field), dict):
                    queue.append((path + '.' + field, node[field], depth + 1))
    return result


def reply_metadata_source():
    return inspect.getsource(safe_reply_metadata)+'\n'+inspect.getsource(reply_explicitly_rejected)


def reply_explicitly_rejected(metadata):
    """Reject known failure fields; never infer success from an HTTP envelope."""
    if not isinstance(metadata,dict):return False
    for node in metadata.get('nodes',[]):
        if not isinstance(node,dict):continue
        if node.get('result') is False:return True
        if type(node.get('code')) is int and 400<=node['code']<=599:return True
        if node.get('status') in {'rejected','error','failed','failure'}:return True
    return False
