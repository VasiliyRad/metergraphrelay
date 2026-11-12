"""Project shared provider outcomes without changing import identity or accounting."""
from dataclasses import asdict
from typing import Any

from ._reliability import classify_model_reliability
from .finish_reason import extract_stop_reason


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
        if choices:
            return {'choices': choices}
    return value


def reliability_fields(*, provider: str | None = None, response: Any = None,
                       error: Any = None, http_status_code: Any = None,
                       finish_reason: str | None = None) -> dict:
    return asdict(classify_model_reliability(provider=provider,
        response=_native_response(response), error=error,
        http_status_code=http_status_code, finish_reason=finish_reason))
