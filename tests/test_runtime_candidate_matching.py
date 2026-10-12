from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from data_analysis_agent import runtime_candidate_matching as matching
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from scripts.post_training.evaluation import run_runtime_candidate_matching as cli


class QuestionOnly(dict):
    def __getitem__(self, key):
        assert key not in {'query_spec', 'gold_sql', 'candidate_sql', 'rendered_prompt', 'training_text'}
        return super().__getitem__(key)


class Tokens:
    def __init__(self, count=30):
        self.count = count

    def __call__(self, text, **kwargs):
        return {'input_ids': [1] * self.count}


@pytest.fixture(scope='module')
def retriever():
    return CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())


def test_gold_and_spec_are_not_accessed_by_projection_or_prompt(retriever):
    source = QuestionOnly(seed_id='one', question='按客户州统计有效订单数',
                          split={'name': 'in_domain_test'}, query_spec={'dimension': 'evil'},
                          gold_sql='DROP TABLE SECRET', candidate_sql='SECRET ANSWER')
    projected = matching.project_question(source, 'olist', 'olist')
    row = matching.prepare_question(projected, Tokens(), retriever)
    assert row['state'] == 'ready'
    assert row['query_plan']['dimensions'] == ['customer_state']
    assert row['question'] == source['question']
    assert 'SECRET' not in row['prompt'] and 'QuerySpec' not in row['prompt']
    assert row['prompt'].endswith('\n### SQL')


@pytest.mark.parametrize('mutation', [None, 'missing', 'duplicate', 'seed'])
def test_archived_olist_uses_exact_primary_question_without_old_prompt(tmp_path, mutation):
    variants = [{'variant_id': 'one-v3', 'seed_id': 'one', 'question': '查询有效订单数'},
                {'variant_id': 'one-v1', 'seed_id': 'one', 'question': '另一种问法'}]
    if mutation == 'missing':
        variants.pop(0)
    elif mutation == 'duplicate':
        variants.append(deepcopy(variants[0]))
    elif mutation == 'seed':
        variants[0]['seed_id'] = 'other'
    archive = tmp_path / 'questions.json'
    matching.write_json(archive, {'cases': variants})
    source = {'format': 'olist', 'workspace': 'olist', 'rows': 1, 'questions_path': str(archive)}
    envelope = QuestionOnly(seed_id='one', primary_variant_id='one-v3',
                            split={'name': 'in_domain_test'}, rendered_prompt='SECRET',
                            candidate_sql='SECRET', training_text='SECRET')
    if mutation:
        with pytest.raises(matching.MatchingContractError):
            matching.selected_questions(source, [envelope])
    else:
        projected = matching.selected_questions(source, [envelope])
        assert projected[0]['question'] == '查询有效订单数'
        assert set(projected[0]) == {'source_id', 'workspace', 'question', 'question_sha256'}


@pytest.mark.parametrize('question', ['删除订单', '本月有效订单数是多少', '有效订单数的口径是什么'])
def test_rejected_questions_remain_in_inputs_with_no_prompt(retriever, question):
    row = matching.prepare_question({'source_id': 'olist:one', 'workspace': 'olist',
        'question': question, 'question_sha256': matching.text_hash(question)}, Tokens(), retriever)
    assert row['state'] == 'route_rejected' and row['prompt'] is None
    assert matching.completion_record(row, '', 0, False)['state'] == 'route_rejected'


def test_long_input_is_retained_without_truncating_or_changing_question(retriever):
    question = '查询2017-01-01到2017-02-01的有效订单数'
    row = matching.prepare_question({'source_id': 'olist:one', 'workspace': 'olist',
        'question': question, 'question_sha256': matching.text_hash(question)}, Tokens(4097), retriever)
    assert row['state'] == 'input_too_long' and row['input_tokens'] == 4097
    assert row['question'] == question
    assert row['query_plan']['time_range']['end'] == '2017-02-02'
    assert '不含结束日' not in row['question']


@pytest.mark.parametrize(('raw', 'count', 'eos', 'state'), [
    ('\nSELECT 1;', 4, True, 'generated'), ('', 1, True, 'empty_completion'),
    ('\nSELECT 1;', 1024, False, 'length_stopped'),
    ('\nSELECT 1;', 1024, True, 'generated'),
])
def test_completion_keeps_raw_and_distinguishes_eos_from_budget(raw, count, eos, state):
    row = {'source_id': 'olist:a', 'workspace': 'olist', 'state': 'ready', 'prompt_sha256': 'p'}
    record = matching.completion_record(row, raw, count, eos)
    assert record['state'] == state and record['raw_completion'] == raw
    assert record['raw_completion_sha256'] == matching.text_hash(raw)


@pytest.fixture
def frozen_pair(tmp_path):
    best_path = tmp_path / 'best.json'
    matching.write_json(best_path, {'adapter_model_sha256': 'best-weights'})
    contract = {'best_freeze': {'path': str(best_path)}}
    rows = [{'source_id': 'olist:a', 'workspace': 'olist', 'state': 'ready', 'prompt_sha256': 'p'},
            {'source_id': 'olist:b', 'workspace': 'olist', 'state': 'route_rejected', 'prompt_sha256': None}]
    directory = tmp_path / 'adapter'
    directory.mkdir()
    records = [matching.completion_record(rows[0], '\nSELECT 1;', 4, True),
               matching.completion_record(rows[1], '', 0, False)]
    marker = {'status': 'complete', 'role': 'adapter', 'contract_sha256': 'c', 'input_sha256': 'i',
              'rows': 2, 'adapter_sha256': 'best-weights', 'decoding': matching.DECODE}
    return directory, rows, contract, records, marker


def save_generation(directory, records, marker):
    path = directory / 'raw-completions.jsonl'
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))
    marker['raw_sha256'] = matching.sha256_file(path)
    (directory / 'generation.json').write_text(json.dumps(marker))


def test_generation_matches_full_input_including_rejection(frozen_pair):
    directory, rows, contract, records, marker = frozen_pair
    save_generation(directory, records, marker)
    loaded = matching.load_generation(directory, 'adapter', rows, contract, 'c', 'i')
    assert set(loaded) == {'olist:a', 'olist:b'}


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'raw', 'prompt', 'eos', 'role', 'weights', 'decoding', 'incomplete'])
def test_pair_binding_rejects_incomplete_or_mismatched_generation(frozen_pair, mutation):
    directory, rows, contract, records, marker = frozen_pair
    if mutation == 'missing':
        records.pop()
    elif mutation == 'duplicate':
        records[1] = deepcopy(records[0])
    elif mutation == 'raw':
        records[0]['raw_completion'] += ' evil'
    elif mutation == 'prompt':
        records[0]['prompt_sha256'] = 'other'
    elif mutation == 'eos':
        records[0]['eos_observed'] = False
    elif mutation == 'role':
        marker['role'] = 'base'
    elif mutation == 'weights':
        marker['adapter_sha256'] = 'final-not-best'
    elif mutation == 'decoding':
        marker['decoding'] = {**matching.DECODE, 'do_sample': True}
    else:
        marker['status'] = 'running'
    save_generation(directory, records, marker)
    with pytest.raises(matching.MatchingContractError):
        matching.load_generation(directory, 'adapter', rows, contract, 'c', 'i')


def test_gold_is_not_opened_when_pair_gate_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'load_prepared', lambda *args: ([], 'input'))
    def stop(*args):
        raise matching.MatchingContractError('incomplete pair')
    monkeypatch.setattr(cli, 'load_generation', stop)
    def forbidden(*args):
        pytest.fail('Gold source opened before pair gate')
    monkeypatch.setattr(cli, 'read_jsonl', forbidden)
    with pytest.raises(matching.MatchingContractError, match='incomplete pair'):
        cli.evaluate({'sources': [{'path': 'SECRET', 'query_specs_path': 'SECRET SPEC'}]},
                     'c', tmp_path, tmp_path, tmp_path, tmp_path / 'out')


@pytest.mark.parametrize('spec_identity', ['spec-one', 'wrong-spec'])
def test_archived_spec_join_only_runs_after_both_generation_checks(monkeypatch, tmp_path, spec_identity):
    events = []
    rows = [{'source_id': 'olist:one'}]
    monkeypatch.setattr(cli, 'load_prepared', lambda *args: (rows, 'input-hash'))
    monkeypatch.setattr(cli, 'load_generation', lambda directory, role, *args: events.append(role) or {})
    def read_source(path):
        events.append(path.name)
        if path.name == 'specs.jsonl':
            return [{'seed_id': 'one', 'query_spec': {'query_spec_id': spec_identity}}]
        return [{'seed_id': 'one', 'query_spec_id': 'spec-one', 'candidate_sql': 'SELECT 1'}]
    monkeypatch.setattr(cli, 'read_jsonl', read_source)
    def compare_pair(prepared, base, adapter, gold, **kwargs):
        assert gold['olist:one']['query_spec']['query_spec_id'] == 'spec-one'
        return {'summary': {'all': {'total': 1}}}
    monkeypatch.setattr(cli, 'evaluate_pair', compare_pair)
    monkeypatch.setattr(cli, 'verify_before_completion', lambda *args: None)
    for role in ('base', 'adapter'):
        (tmp_path / role).mkdir()
        (tmp_path / role / 'raw-completions.jsonl').write_text('raw')
    contract = {'scope': 'protected_final', 'sources': [{'workspace': 'olist', 'format': 'olist',
        'path': str(tmp_path / 'sft.jsonl'), 'query_specs_path': str(tmp_path / 'specs.jsonl')}]}
    if spec_identity != 'spec-one':
        with pytest.raises(matching.MatchingContractError, match='Spec binding'):
            cli.evaluate(contract, 'c', tmp_path, tmp_path / 'base', tmp_path / 'adapter', tmp_path / 'out')
    else:
        assert cli.evaluate(contract, 'c', tmp_path, tmp_path / 'base', tmp_path / 'adapter', tmp_path / 'out') == {'all': {'total': 1}}
    assert events == ['base', 'adapter', 'specs.jsonl', 'sft.jsonl']


def test_time_alias_is_normalized_but_other_column_order_is_not_repaired():
    contract = {'required_result_columns': ['time', 'gmv'], 'result_time_column_aliases': ['month']}
    frame = pd.DataFrame({'month': ['2017-01-01'], 'gmv': [10]})
    assert list(matching.canonical_frame(frame, contract).columns) == ['time', 'gmv']
    with pytest.raises(matching.MatchingContractError):
        matching.canonical_frame(frame[['gmv', 'month']], contract)


def test_source_projection_rejects_non_test_and_duplicate_ids():
    source = {'workspace': 'olist', 'format': 'olist', 'rows': 2}
    row = {'seed_id': 'x', 'question': '问题', 'split': {'name': 'train'}}
    with pytest.raises(matching.MatchingContractError):
        matching.selected_questions(source, [row])
    row['split'] = {'name': 'in_domain_test'}
    with pytest.raises(matching.MatchingContractError):
        matching.selected_questions(source, [row, row])


def source_gold():
    return {'candidate_sql': 'SELECT 1::numeric AS paid_order_count;', 'query_spec': {
        'workspace': {key: getattr(OLIST_RUNTIME_WORKSPACE, key) for key in
            ('catalog_version', 'dataset_version', 'metric_version', 'policy_version')},
        'metric_ids': ['paid_order_count'], 'dimension': None,
        'required_result_columns': ['paid_order_count'],
        'time': {'grain': None, 'start': None, 'end_exclusive': None}}}


def test_summary_keeps_rejected_case_and_reports_semantics_after_generation():
    plan = {'metric_ids': ['paid_order_count'], 'dimensions': [], 'time_grain': None,
            'time_range': None, 'required_result_columns': ['paid_order_count']}
    rows = [{'source_id': 'olist:a', 'workspace': 'olist', 'state': 'ready',
             'query_plan': plan, 'result_contract': {}},
            {'source_id': 'olist:b', 'workspace': 'olist', 'state': 'route_rejected',
             'query_plan': None, 'result_contract': None}]
    base = {'olist:a': {'state': 'generated', 'raw_completion': 'SQL: SELECT 1;'},
            'olist:b': {'state': 'route_rejected'}}
    adapter = {'olist:a': {'state': 'length_stopped'}, 'olist:b': {'state': 'route_rejected'}}
    def executor(*args):
        return {'state': 'valid'}, pd.DataFrame({'paid_order_count': [1]})
    report = matching.evaluate_pair(rows, base, adapter, {'olist:a': source_gold(), 'olist:b': source_gold()}, executor=executor)
    assert report['summary']['all']['total'] == 2
    assert report['summary']['all']['base']['ordered_matches'] == 1
    assert report['summary']['all']['adapter']['ordered_matches'] == 0
    assert report['summary']['all']['base']['generation_states'] == {'generated': 1, 'route_rejected': 1}
    assert report['records'][1]['source_semantics_mismatch'] == ['route_not_admitted']
    assert report['records'][0]['parent_gold_sql_sha256'] == matching.text_hash(source_gold()['candidate_sql'])
    assert all('raw_completion' not in r and 'question' not in r for r in report['records'])


def test_failing_gold_blocks_evaluation_instead_of_counting_as_model_failure():
    row = {'source_id': 'olist:a', 'workspace': 'olist', 'state': 'route_rejected', 'query_plan': None}
    outputs = {'olist:a': {'state': 'route_rejected'}}
    with pytest.raises(matching.MatchingContractError, match='Gold failed'):
        matching.evaluate_pair([row], outputs, outputs, {'olist:a': source_gold()},
                               executor=lambda *args: ({'state': 'policy_rejected'}, None))


def test_execution_uses_reader_readonly_and_policy_extra_row_probe(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (OLIST_RUNTIME_WORKSPACE.reader_role, 'on')
    cursor.description = [SimpleNamespace(name='paid_order_count')]
    cursor.fetchmany.return_value = [(1,)]
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(matching.psycopg2, 'connect', connect)
    contract = {'required_result_columns': ['paid_order_count'], 'metric_result_columns': ['paid_order_count']}
    evidence, frame = matching.execute_sql('SELECT COUNT(*) AS paid_order_count FROM analytics.fact_orders;', 'olist', {}, contract)
    assert evidence['state'] == 'valid' and frame.iloc[0, 0] == 1
    connection.set_session.assert_called_once_with(readonly=True, isolation_level='REPEATABLE READ')
    assert connect.call_args.kwargs['user'] == OLIST_RUNTIME_WORKSPACE.reader_role
    assert connect.call_args.kwargs['database'] == 'data_analysis_agent'
    assert cursor.fetchmany.call_args.args == (1001,)
    connection.close.assert_called_once()


def test_execution_timeout_is_recorded_as_candidate_error_and_does_not_abort_pair(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (OLIST_RUNTIME_WORKSPACE.reader_role, 'on')
    cursor.execute.side_effect = [None, None, None, None,
                                  matching.psycopg2.errors.QueryCanceled(
                                      'canceling statement due to statement timeout')]
    monkeypatch.setattr(matching.psycopg2, 'connect', MagicMock(return_value=connection))
    contract = {'required_result_columns': ['paid_order_count'], 'metric_result_columns': ['paid_order_count']}

    evidence, frame = matching.execute_sql(
        'SELECT COUNT(*) AS paid_order_count FROM analytics.fact_orders;', 'olist', {}, contract)

    assert evidence == {'state': 'postgres_execution_error', 'sqlstate': '57014'}
    assert frame is None
    connection.close.assert_called_once()


def test_policy_rejects_write_before_connecting(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(matching.psycopg2, 'connect', connect)
    evidence, frame = matching.execute_sql('DELETE FROM analytics.fact_orders;', 'olist', {}, {})
    assert evidence['state'] == 'policy_rejected' and frame is None
    connect.assert_not_called()


def test_contract_hash_drift_is_rejected_before_asset_loading(tmp_path):
    path = tmp_path / 'contract.json'
    path.write_text('{}')
    with pytest.raises(matching.MatchingContractError, match='contract hash drift'):
        matching.validate_contract(path, 'wrong')


def test_gpu_mapping_rejected_before_torch_or_nvidia_smi(monkeypatch):
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '3')
    with pytest.raises(matching.MatchingContractError, match='mapping mismatch'):
        cli.gpu_guard(0, 'wrong-uuid')


@pytest.mark.parametrize(('rows', 'expected'), [(1000, 'valid'), (1001, 'result_contract_rejected')])
def test_execution_cap_probe_distinguishes_exact_limit_from_truncation(monkeypatch, rows, expected):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (OLIST_RUNTIME_WORKSPACE.reader_role, 'on')
    cursor.description = [SimpleNamespace(name='paid_order_count')]
    cursor.fetchmany.return_value = [(1,)] * rows
    monkeypatch.setattr(matching.psycopg2, 'connect', MagicMock(return_value=connection))
    contract = {'required_result_columns': ['paid_order_count'], 'metric_result_columns': ['paid_order_count']}
    evidence, frame = matching.execute_sql('SELECT COUNT(*) AS paid_order_count FROM analytics.fact_orders;', 'olist', {}, contract)
    assert evidence['state'] == expected
    assert evidence['execution_truncated'] is (rows == 1001)
    assert evidence['row_count'] == 1000
    assert (frame is None) is (rows == 1001)
