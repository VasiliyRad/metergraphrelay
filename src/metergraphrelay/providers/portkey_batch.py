"""Identify Anthropic batch operations and expand their individual outcomes.

The download is transport, not inference: its cost, latency and token counters
cannot be assigned to any child message. Successful message IDs are the stable
identity even when an application downloads the same results more than once.
"""
from __future__ import annotations

import json
import re
from typing import Any


class BatchShapeError(ValueError):
    pass


_CONTROL = re.compile(r"(?:anthropic/)?(?:batches|msgbatch_[A-Za-z0-9_-]+)(?:/cancel)?\Z")
_RESULTS = re.compile(r"(?:anthropic/)?(?:results|msgbatch_[A-Za-z0-9_-]+/results)\Z")
_RESULT_BATCH = re.compile(r"(?:anthropic/)?(?P<batch>msgbatch_[A-Za-z0-9_-]+)/results\Z")


def _validate_cache(usage: dict) -> None:
    for key in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
            raise BatchShapeError('invalid batch message cache usage')
    if 'cache_creation' in usage:
        creation = usage['cache_creation']
        if not isinstance(creation, dict):
            raise BatchShapeError('invalid batch message cache usage')
        for key in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'):
            if key in creation and (type(creation[key]) is not int or creation[key] < 0):
                raise BatchShapeError('invalid batch message cache usage')


def _envelopes(response: Any) -> list[dict]:
    if isinstance(response, str):
        try:
            response = [json.loads(line) for line in response.splitlines() if line.strip()]
        except (ValueError, RecursionError):
            raise BatchShapeError('invalid Anthropic batch results JSONL') from None
        if len(response) == 1 and isinstance(response[0], list):
            response = response[0]
    if isinstance(response, dict):
        response = [response]
    if not isinstance(response, list) or not response:
        raise BatchShapeError('Anthropic batch results must contain outcomes')
    if not all(isinstance(item, dict) for item in response):
        raise BatchShapeError('invalid Anthropic batch result envelope')
    return response


def expand_batch(row: dict, *, batch_requests: dict | None = None) -> list[dict] | None:
    """Return ordinary-shaped Portkey child rows, [] for control, None otherwise.

    Request correlation is supplied by a caller that knows the batch identity.
    Joining custom_id across an arbitrary export would mix unrelated batches.
    """
    if str(row.get('ai_org', '')).strip().lower() != 'anthropic':
        return None
    response = row.get('response')
    model = str(row.get('ai_model') or '')
    if (isinstance(response, dict) and response.get('type') == 'message_batch') or _CONTROL.fullmatch(model):
        return []
    envelope = isinstance(response, dict) and 'custom_id' in response and 'result' in response
    if not envelope and not _RESULTS.fullmatch(model):
        return None
    if not isinstance(row.get('response_status_code'), int) or row['response_status_code'] >= 400:
        raise BatchShapeError('Anthropic batch results download failed')

    expanded = []
    seen = set()
    for item in _envelopes(response):
        custom_id = item.get('custom_id')
        outcome = item.get('result')
        if not isinstance(custom_id, str) or not custom_id or not isinstance(outcome, dict):
            raise BatchShapeError('invalid Anthropic batch result envelope')
        if custom_id in seen:
            raise BatchShapeError('duplicate custom_id in Anthropic batch results')
        seen.add(custom_id)
        request = (batch_requests or {}).get(custom_id)
        if request is not None and not isinstance(request, dict):
            raise BatchShapeError('invalid correlated Anthropic batch request')
        outcome_type = outcome.get('type')
        if outcome_type == 'succeeded':
            message = outcome.get('message')
            if not isinstance(message, dict) or message.get('type') != 'message':
                raise BatchShapeError('successful batch outcome is missing a message')
            message_id, actual_model, usage = message.get('id'), message.get('model'), message.get('usage')
            if not isinstance(message_id, str) or not message_id.strip():
                raise BatchShapeError('successful batch message is missing its identity')
            if not isinstance(actual_model, str) or not actual_model.strip() or not isinstance(usage, dict):
                raise BatchShapeError('successful batch message is missing model or usage')
            for field in ('input_tokens', 'output_tokens'):
                if type(usage.get(field)) is not int or usage[field] < 0:
                    raise BatchShapeError('invalid batch message token usage')
            _validate_cache(usage)
            identity = 'anthropic-batch:' + message_id.strip()
            status = 200
        elif outcome_type in ('errored', 'canceled', 'expired'):
            actual_model = request.get('model') if request else None
            usage = {}
            batch_id = row.get('batch_id')
            endpoint_batch = _RESULT_BATCH.fullmatch(model)
            if not batch_id and endpoint_batch:
                batch_id = endpoint_batch['batch']
            # Without a provider batch ID, the export ID is the only available
            # stable boundary. Never deduplicate unrelated batches by custom_id.
            boundary = batch_id if isinstance(batch_id, str) and batch_id else row.get('id')
            if not isinstance(boundary, str) or not boundary.strip():
                raise BatchShapeError('failed batch outcome is missing its identity')
            identity = 'anthropic-batch:' + boundary.strip() + ':' + custom_id
            message = {'error': outcome.get('error') or {'type': outcome_type, 'message': outcome_type}}
            status = 400
        else:
            raise BatchShapeError('unknown Anthropic batch outcome type')
        expanded.append({
            **row, 'id': identity, 'ai_model': actual_model, 'request': request,
            'response': message, 'response_status_code': status,
            'req_units': usage.get('input_tokens'), 'res_units': usage.get('output_tokens'),
            'cost': None, 'response_time': None, 'batch_custom_id': custom_id,
            'batch_outcome': outcome_type,
        })
    return expanded
