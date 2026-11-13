"""Evidence extraction must not change import identity or accounting."""

import pytest
from metergraphrelay.providers.portkey import normalize_portkey_row

@pytest.mark.parametrize('status,response,error_code,signal', [
    (429, {'error': {'code': 'rate_limit_exceeded'}}, 'rate_limit_exceeded', None),
    (503, {'error': {'message': 'retry later'}}, None, None),
    (401, {'error': {'code': 'invalid_api_key'}}, 'invalid_api_key', None),
    (200, {'choices': [{'finish_reason': 'content_filter','message': {'content': None}}]}, None, None),
    (200, {'choices': [{'finish_reason': 'stop','message': {'content': 'ok', 'refusal': None}}]}, None, False),
    (200, {'usage': {'total_tokens': 4}}, None, None),
])
def test_portkey_evidence_preserves_identity_and_accounting(status,response,error_code,signal):
    source = {'id':'request-1','trace_id':'trace-1','created_at':'2026-11-01T00:00:00Z',
              'ai_org':'openai','ai_model':'example-model','cost':1,'req_units':3,'res_units':1,
              'response_time':25,'response_status_code':status,'request':{},'response':response}
    row = normalize_portkey_row(source)
    assert row['http_status_code'] == status
    assert row['error_code'] == error_code
    assert row['refusal_signal'] is signal
    assert 'availability' not in row and 'refused' not in row
    assert row['request_id'] == 'request-1'
    assert row['input_tokens'] == 3 and row['output_tokens'] == 1
    assert row['cost_usd'] == '0.01'
    assert row['latency_ms'] == 25


def test_other_import_does_not_infer_non_refusal_from_success():
    from metergraphrelay.providers.braintrust import normalize_span
    row = normalize_span({'id':'span-1','created':'2026-11-01T00:00:00Z',
        'metadata': {'model':'example-model'}, 'metrics': {}, 'output': 'ok',
        'span_attributes': {'type':'llm'}, 'input': 'input'}, route_override=None)
    assert row['refusal_signal'] is None
    assert row['response_received'] is None
    assert 'availability' not in row and 'refused' not in row


def test_langchain_refusal_is_preserved_before_content_conversion():
    from metergraphrelay.reliability import reliability_fields
    output = {'generations': [[{'generation_info': {'finish_reason': 'stop'},
        'message': {'kwargs': {'content': '', 'additional_kwargs': {'refusal': 'policy'}}}}]]}
    assert reliability_fields(response=output, finish_reason='stop')['refusal_signal'] is True


def test_list_message_refusal_is_preserved_before_content_conversion():
    from metergraphrelay.reliability import reliability_fields
    assert reliability_fields(response=[{'role':'assistant','content':None,'refusal':'policy'}])['refusal_signal'] is True


def test_all_import_adapters_preserve_explicit_refusal():
    from types import SimpleNamespace as NS
    from metergraphrelay.providers.langfuse import normalize_observation
    from metergraphrelay.providers.langsmith import normalize_run
    from metergraphrelay.providers.braintrust import normalize_span as braintrust_span
    from metergraphrelay.providers.phoenix import normalize_span as phoenix_span
    from metergraphrelay.providers.openai import normalize_completion
    response = {'choices':[{'finish_reason':'stop','message':{'content':None,'refusal':'policy'}}]}
    rows = [
        normalize_observation({'id':'obs-1','traceId':'trace-1','startTime':'2026-11-01T00:00:00Z',
            'model':'example-model','output':response},route_override=None),
        normalize_run({'id':'run-1','start_time':'2026-11-01T00:00:00Z',
            'outputs':{'generations':[[{'generation_info':{'finish_reason':'stop'},'message':{
                'kwargs':{'content':'','additional_kwargs':{'refusal':'policy'}}}}]]}},route_override=None),
        braintrust_span({'id':'span-1','created':'2026-11-01T00:00:00Z','metrics':{},
            'output':[{'role':'assistant','content':None,'refusal':'policy'}]},route_override=None),
        phoenix_span({'id':'span-1','start_time':'2026-11-01T00:00:00Z', 'status_code':'ERROR',
            'attributes':{'gen_ai.response.finish_reasons':['stop', 'content_filter'],
                          'error.type':'provider_policy_block'}},project='synthetic',route_override=None),
        normalize_completion(NS(created=1,id='completion-1',model='example-model',
            choices=[NS(finish_reason='stop',message=NS(content=None,refusal='policy'))]), [],
            route='synthetic',include_content=False),
    ]
    for row in rows:
        assert 'availability' not in row and 'refused' not in row
    for row in rows[:3] + rows[4:]:
        assert row['refusal_signal'] is True
    assert rows[3]['refusal_signal'] is None

    assert rows[-2]['error_type'] == 'provider_policy_block'
    assert rows[-2]['otel']['finish_reasons'] == ['stop', 'content_filter']


def test_sparse_and_multi_choice_evidence_stays_conservative():
    from metergraphrelay.reliability import reliability_fields
    assert reliability_fields(response={'choices': [{'message': {'content': 'ok'}}]})['refusal_signal'] is None
    assert reliability_fields(response=[{'role': 'assistant', 'content': 'ok', 'finish_reason': 'stop'}, {}])['refusal_signal'] is None
    assert reliability_fields(response={'finish_reasons': ['stop', 'content_filter']}, finish_reason='stop')['otel']['finish_reasons'] == ['stop', 'content_filter']
