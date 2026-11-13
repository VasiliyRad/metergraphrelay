"""Extract content-free provider facts for receiver-owned classification."""
from typing import Any

from .finish_reason import extract_finish_reasons, extract_stop_reason


def _native_response(value: Any) -> Any:
    """Unwrap documented message/generation containers before text extraction."""
    if isinstance(value, dict):
        if any(key in value for key in ('choices', 'candidates', 'output', 'stop_reason')):
            return value
        if 'generations' in value:
            return _native_response(value['generations'])
        if 'message' in value:
            message = value['message']
            message = message.get('kwargs', message) if isinstance(message, dict) else message
            result = _native_response(message)
            if isinstance(result, dict) and 'choices' in result:
                finish = extract_stop_reason(value)
                for choice in result['choices']:
                    choice['finish_reason'] = finish or choice.get('finish_reason')
            return result
        if value.get('role') == 'assistant' or 'additional_kwargs' in value:
            message = dict(value)
            extra = value.get('additional_kwargs')
            if isinstance(extra, dict) and extra.get('refusal') is not None:
                message['refusal'] = extra['refusal']
            return {'choices': [{'message': message, 'finish_reason': extract_stop_reason(value)}]}
    if isinstance(value, (list, tuple)):
        choices = []
        for item in value:
            native = _native_response(item)
            if isinstance(native, dict) and isinstance(native.get('choices'), list):
                choices.extend(native['choices'])
            else:
                choices.append({})
        if any(choice for choice in choices):
            return {'choices': choices}
    return value



def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _items(value: Any) -> list | tuple:
    return value if isinstance(value, (list, tuple)) else ()


def _string(value: Any) -> str | None:
    return value[:256] if isinstance(value, str) and value else None


def reliability_fields(*, provider: str | None = None, response: Any = None,
                       error: Any = None, http_status_code: Any = None,
                       finish_reason: str | None = None,
                       attributes: dict | None = None) -> dict:
    """Keep native signals before text conversion; do not infer provider outcomes."""
    source_reasons = extract_finish_reasons(response)
    response = _native_response(response)
    attributes = attributes or {}
    payload = _get(error, 'error', _get(error, 'body', error))
    payload = _get(payload, 'error', payload)
    code = http_status_code
    if code is None:
        code = attributes.get('http.response.status_code', attributes.get('http.status_code'))
    if code is None:
        code = (_get(error, 'status_code') or _get(error, 'status') or _get(error, 'statusCode')
                or _get(_get(error, 'response'), 'status_code') or _get(response, 'status_code'))
    reasons = []
    def reason(value):
        value = _string(value)
        if value is not None:
            reasons.append(value)
    for value in _items(attributes.get('gen_ai.response.finish_reasons')):
        reason(value)
    if not reasons:
        reason(attributes.get('llm.finish_reason'))
    if not reasons:
        reasons.extend(source_reasons)
    if not reasons:
        reason(_get(response, 'stop_reason'))
        reason(_get(response, 'finish_reason'))
        reason(_get(_get(response, 'incomplete_details'), 'reason'))
    feedback = _get(response, 'promptFeedback', _get(response, 'prompt_feedback'))
    reason(_get(feedback, 'blockReason', _get(feedback, 'block_reason')))
    signal = None
    received = (any(isinstance(_get(response, key), (list, tuple)) for key in ('choices', 'candidates', 'output'))
                or feedback is not None
                or _get(response, 'stop_reason') is not None and isinstance(_get(response, 'content'), (list, tuple)))
    inspected = []
    for choice in _items(_get(response, 'choices')):
        if not source_reasons:
            reason(_get(choice, 'finish_reason'))
        message = _get(choice, 'message')
        if _get(message, 'refusal'):
            signal = True
        inspected.append(message is not None and _string(_get(choice, 'finish_reason')) is not None and (
            _get(message, 'content') is not None or _get(message, 'tool_calls') is not None
            or isinstance(message, dict) and 'refusal' in message
            or not isinstance(message, dict) and hasattr(message, 'refusal')))
    output = _get(response, 'output')
    for item in _items(output):
        parts = _get(item, 'content')
        kind = _get(item, 'type')
        if kind == 'refusal' or any(_get(part, 'type') == 'refusal' for part in _items(parts)):
            signal = True
        inspected.append(_get(response, 'status') in {'completed', 'incomplete'} and (
            kind in {'function_call', 'reasoning'} or (
                kind == 'message' and bool(_items(parts)) and all(
                    _get(part, 'type') in {'output_text', 'refusal'} for part in _items(parts)))))
    content = _get(response, 'content')
    if _get(response, 'stop_reason') is not None and isinstance(content, (list, tuple)):
        if any(_get(part, 'type') == 'refusal' for part in content):
            signal = True
        inspected.append(bool(content) and all(_get(part, 'type') in {
            'text', 'tool_use', 'thinking', 'redacted_thinking', 'refusal'} for part in content))
    for candidate in _items(_get(response, 'candidates')):
        if not source_reasons:
            reason(_get(candidate, 'finishReason', _get(candidate, 'finish_reason')))
        inspected.append(_string(_get(candidate, 'finishReason', _get(candidate, 'finish_reason'))) is not None
                         and bool(_items(_get(_get(candidate, 'content'), 'parts'))))
    if not reasons:
        reason(finish_reason)
    if signal is None and inspected and all(inspected):
        signal = False
    row = {
        'http_status_code': code if type(code) is int and 100 <= code <= 599 else None,
        'error_code': _string(_get(payload, 'code')),
        'refusal_signal': signal,
        'response_received': True if received else None,
    }
    error_type = attributes.get('error.type') or _get(payload, 'type') or _get(error, 'name')
    if _string(error_type):
        row['error_type'] = _string(error_type)
    if reasons:
        row['otel'] = {'finish_reasons': reasons}
    return row
