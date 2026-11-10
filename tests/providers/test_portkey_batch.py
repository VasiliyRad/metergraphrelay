import copy
import json

import pytest

from metergraphrelay.providers import portkey
from metergraphrelay.import_identity import ImportContext


def message(identity='msg_example', model='claude-sonnet-4-6'):
    return {
        'id': identity, 'type': 'message', 'role': 'assistant', 'model': model,
        'content': [{'type': 'text', 'text': 'Synthetic answer'}],
        'stop_reason': 'end_turn',
        'usage': {'input_tokens': 100, 'output_tokens': 50,
                  'cache_read_input_tokens': 40, 'cache_creation_input_tokens': 20,
                  'cache_creation': {'ephemeral_5m_input_tokens': 20,
                                     'ephemeral_1h_input_tokens': 0}},
    }


def result(custom_id='custom-example', **kw):
    return {'custom_id': custom_id,
            'result': {'type': 'succeeded', 'message': message(**kw)}}


def export_row(response=None, **kw):
    return {'id': 'download-example', 'trace_id': 'trace-example',
            'created_at': '2026-01-01T12:00:00Z',
            'ai_org': 'anthropic', 'ai_model': 'anthropic/results',
            'request': {}, 'response': result() if response is None else response,
            'response_status_code': 200, 'response_time': 42,
            'req_units': 0, 'res_units': 0, 'cost': 99,
            'metadata': {'workflow_name': 'synthetic-workflow'}, **kw}


def normalize(row, **kw):
    return portkey.normalize_portkey_rows(row, **kw)


@pytest.mark.parametrize('model', ['anthropic/batches', 'anthropic/msgbatch_example'])
def test_management_is_not_an_inference(model):
    assert normalize(export_row(ai_model=model, response={
        'id': 'msgbatch_example', 'type': 'message_batch',
        'processing_status': 'in_progress'})) == []


def test_batch_result_uses_native_message_and_batch_pricing():
    source = export_row()
    before = copy.deepcopy(source)
    row, = normalize(source, import_context=ImportContext('portkey', 'scope-example'))
    assert source == before
    assert row['model'] == 'claude-sonnet-4-6'
    assert row['batch'] is True
    assert row['endpoint'] == 'messages'
    assert row['input_tokens'] == 100  # native input excludes cache
    assert row['output_tokens'] == 50
    assert row['cache_read_tokens'] == 40
    assert row['cache_write_tokens'] == 20
    assert row['cache_write_5m_tokens'] == 20
    assert row['response_text'] == 'Synthetic answer'
    assert row['stop_reason'] == 'end_turn'
    assert row['status'] == 'success'
    assert row.get('request_json') is None
    assert not row.get('latency_ms')
    for key in ('cost_usd', 'reported_cost_usd', 'reported_cost_source'):
        assert row.get(key) is None
    assert row['import_event_id'] == 'anthropic-batch:msg_example'


def test_repeated_download_has_same_inference_identity():
    context = ImportContext('portkey', 'scope-example')
    a, = normalize(export_row(), import_context=context)
    b, = normalize(export_row(id='download-again'), import_context=context)
    assert a['import_event_id'] == b['import_event_id']
    assert a['request_id'] == b['request_id']


@pytest.mark.parametrize('encoding', ['list', 'jsonl'])
def test_multiresult_mixed_models_and_order(encoding):
    envelopes = [result('second', identity='msg_second', model='claude-opus-5'),
                 result('first', identity='msg_first')]
    response = envelopes if encoding == 'list' else '\n'.join(map(json.dumps, envelopes))
    rows = normalize(export_row(response=response), batch_requests={
        'first': {'model': 'claude-sonnet-4-6', 'messages': [{'role': 'user', 'content': 'A'}]},
        'second': {'model': 'claude-opus-5', 'messages': [{'role': 'user', 'content': 'B'}]},
    })
    assert [row['model'] for row in rows] == ['claude-opus-5', 'claude-sonnet-4-6']
    assert [json.loads(row['request_json'])['messages'][0]['content'] for row in rows] == ['B', 'A']


@pytest.mark.parametrize('response', ['broken json', '{"custom_id":"a","result":',
                                     {'custom_id': 'a', 'result': {'type': 'succeeded'}},
                                     {'custom_id': 'a', 'result': {'type': 'unexpected'}}])
def test_malformed_result_fails_window(response):
    with pytest.raises(portkey.PortkeyConversionError):
        normalize(export_row(response=response))


def test_partial_jsonl_is_not_silently_dropped():
    with pytest.raises(portkey.PortkeyConversionError):
        normalize(export_row(response=json.dumps(result()) + '\nnot-json'))


@pytest.mark.parametrize('outcome', ['errored', 'canceled', 'expired'])
def test_failed_result_is_explicit_and_not_a_success(outcome):
    envelope = {'custom_id': 'custom-example', 'result': {'type': outcome}}
    if outcome == 'errored':
        envelope['result']['error'] = {'type': 'invalid_request_error', 'message': 'Synthetic error'}
    row, = normalize(export_row(response=envelope), batch_requests={
        'custom-example': {'model': 'claude-sonnet-4-6', 'messages': []}})
    assert row['model'] == 'claude-sonnet-4-6'
    assert row['status'] == 'error'
    assert row['error'] is True
    assert row['batch'] is True
    assert row.get('input_tokens') is None
    assert row.get('cost_usd') is None


def test_regular_message_is_unchanged():
    source = export_row(response=message(), ai_model='claude-sonnet-4-6')
    assert normalize(source) == [portkey.normalize_portkey_row(source)]


def test_failed_identity_uses_known_results_endpoint_batch():
    envelope = {'custom_id': 'a', 'result': {'type': 'expired'}}
    context = ImportContext('portkey', 'scope-example')
    first, = normalize(export_row(response=envelope, ai_model='anthropic/msgbatch_known/results'), import_context=context)
    again, = normalize(export_row(response=envelope, ai_model='anthropic/msgbatch_known/results', id='other-download'), import_context=context)
    assert first['import_event_id'] == again['import_event_id'] == 'anthropic-batch:msgbatch_known:a'


@pytest.mark.parametrize('field,value', [
    ('cache_read_input_tokens', -1000), ('cache_creation_input_tokens', True),
    ('cache_read_input_tokens', '40'),
    ('cache_creation', {'ephemeral_5m_input_tokens': -1}),
    ('cache_creation', {'ephemeral_1h_input_tokens': '20'}),
])
def test_invalid_cache_usage_fails_window(field, value):
    envelope = result()
    envelope['result']['message']['usage'][field] = value
    with pytest.raises(portkey.PortkeyConversionError):
        normalize(export_row(response=envelope))


def test_export_converter_excludes_control_and_expands_results(tmp_path):
    source, dest = tmp_path / 'source.jsonl', tmp_path / 'dest.jsonl'
    create = export_row(ai_model='anthropic/batches', response={
        'type': 'message_batch', 'id': 'msgbatch_example'}, request={
        'requests': [{'custom_id': 'custom-example', 'params': {'model': 'claude-sonnet-4-6',
                     'messages': [{'role': 'user', 'content': 'Synthetic request'}]}}]})
    source.write_text(json.dumps(create) + '\n' + json.dumps(export_row()) + '\n')
    count, skipped = portkey.convert_portkey_export(str(source), str(dest))
    assert count == 1
    assert skipped == 0  # intentional lifecycle exclusion is not malformed data
    row, = [json.loads(line) for line in dest.read_text().splitlines()]
    assert row['model'] == 'claude-sonnet-4-6'
    # Exports can contain multiple unrelated batches: no unsafe cross-batch join.
    assert row.get('request_json') is None
