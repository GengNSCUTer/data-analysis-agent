"""Frozen question-only matching protocol; no model, repair or training dependency."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

import pandas as pd
import psycopg2

from .candidate_sql_generator import RUNTIME_CANDIDATE_SQL_PROMPT_VERSION, unwrap_sql_completion
from .metric_context import OLIST_RUNTIME_WORKSPACE
from .plan_sql_guard import sql_plan_mismatch
from .post_training_comparison import sha256_file
from .postgres_runner import cap_probed_rows, execution_probe
from .result_validator import ResultValidator
from .runtime_alignment_audit import compare_source_semantics, replay_single_turn
from .runtime_candidate_materializer import reorder_gold_projection
from .semantic_catalog import CatalogLoader, CatalogRetriever
from .sql_policy import PolicyViolation, SqlPolicy
from .thelook_v2_context import THELOOK_V2_WORKSPACE

VERSION = 'runtime-candidate-v2-matching-v1'
DECODE = {'do_sample': False, 'num_beams': 1, 'max_new_tokens': 1024,
          'max_input_tokens': 4096, 'seed': 20261011, 'use_cache': True}
WORKSPACES = {'olist': OLIST_RUNTIME_WORKSPACE, 'thelook': THELOOK_V2_WORKSPACE}
DATABASES = {'olist': 'data_analysis_agent', 'thelook': 'thelook_analytics'}
READY_STATES = {'ready', 'route_rejected', 'input_too_long'}
GENERATION_STATES = READY_STATES - {'ready'} | {'generated', 'empty_completion', 'length_stopped'}


class MatchingContractError(ValueError):
    """A frozen asset or complete-run invariant was violated."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MatchingContractError(message)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8'))
    require(isinstance(value, dict), 'JSON artifact must be an object')
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
    require(bool(rows) and all(isinstance(row, dict) for row in rows), 'invalid JSONL artifact')
    return rows


def write_json(path: Path, value: Any) -> None:
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')


def project_question(row: Mapping[str, Any], workspace: str, source_format: str) -> dict[str, Any]:
    """Whitelist projection: intentionally never access Spec, labels or old prompts."""
    key = {'olist': 'seed_id', 'thelook': 'case_id', 'runtime': 'sample_id'}[source_format]
    if source_format == 'olist':
        require(isinstance(row.get('split'), Mapping) and row['split'].get('name') == 'in_domain_test',
                'unexpected Olist split')
    elif source_format == 'runtime':
        require(row.get('split') == {'name': 'validation'} and row.get('workspace_key') == workspace,
                'smoke must use the expected validation workspace')
    identity, question = row[key], row['question']
    require(isinstance(identity, str) and bool(identity) and isinstance(question, str)
            and bool(question.strip()), 'source question/identity malformed')
    return {'source_id': workspace + ':' + identity, 'workspace': workspace,
            'question': question, 'question_sha256': text_hash(question)}


def selected_questions(source: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if source['format'] == 'runtime':
        rows = [row for row in rows if row.get('workspace_key') == source['workspace']]
    projected = [project_question(row, source['workspace'], source['format']) for row in rows]
    projected.sort(key=lambda row: row['source_id'])
    require(len({r['source_id'] for r in projected}) == len(projected), 'duplicate source identity')
    if source['format'] == 'runtime':
        projected = projected[:source['rows']]
    require(len(projected) == source['rows'], 'source count differs from frozen contract')
    return projected


def prepare_question(row: Mapping[str, Any], tokenizer: Any, retriever: CatalogRetriever) -> dict[str, Any]:
    replay = replay_single_turn(row['question'], retriever, workspace=WORKSPACES[row['workspace']])
    result = {**row, 'route_state': replay['route_state'], 'route_reason': replay['route_reason'],
              'state': 'route_rejected', 'prompt': None, 'prompt_sha256': None,
              'input_tokens': 0, 'query_plan': None, 'result_contract': None}
    if replay['plan'] is None:
        return result
    prompt = replay['candidate_prompt']
    require(prompt.endswith('\n### SQL'), 'runtime prompt suffix drift')
    prompt_hash = text_hash(prompt)
    require(prompt_hash == replay['candidate_request']['prompt_sha256'], 'builder prompt hash drift')
    count = len(tokenizer(prompt, add_special_tokens=False)['input_ids'])
    result.update(state='input_too_long' if count > DECODE['max_input_tokens'] else 'ready',
                  prompt=prompt, prompt_sha256=prompt_hash, input_tokens=count,
                  query_plan=replay['plan'], result_contract=replay['result_contract'])
    return result


def validate_contract(path: Path, expected_sha256: str) -> dict[str, Any]:
    require(sha256_file(path) == expected_sha256, 'matching contract hash drift')
    contract = read_json(path)
    require(contract.get('version') == VERSION and contract.get('decoding') == DECODE
            and contract.get('prompt_version') == RUNTIME_CANDIDATE_SQL_PROMPT_VERSION
            and contract.get('scope') in {'protected_final', 'validation_smoke'}, 'protocol drift')
    freeze_path = Path(contract['best_freeze']['path'])
    require(sha256_file(freeze_path) == contract['best_freeze']['sha256'], 'best freeze drift')
    best = read_json(freeze_path)
    require(best['selected_step'] == 2500 and best['status'] == 'frozen', 'unexpected selected checkpoint')
    adapter = Path(best['adapter_dir'])
    require(sha256_file(adapter / 'adapter_model.safetensors') == best['adapter_model_sha256']
            and sha256_file(adapter / 'adapter_config.json') == best['adapter_config_sha256'],
            'selected adapter drift')
    for file, expected in contract['asset_sha256'].items():
        require(sha256_file(Path(file)) == expected, 'frozen code/catalog asset drift')
    for source in contract['sources']:
        require(sha256_file(Path(source['path'])) == source['sha256'], 'frozen source file drift')
    model_dir = Path(contract['model_dir'])
    require(sha256_file(model_dir / 'download_manifest.json')
            == best['base_model']['download_manifest_sha256'], 'base manifest drift')
    manifest = read_json(model_dir / 'download_manifest.json')
    require(manifest['model_id'] == best['base_model']['id']
            and manifest['revision'] == best['base_model']['revision'], 'base identity drift')
    for item in manifest['files']:
        file = (model_dir / item['path']).resolve()
        require(file.is_relative_to(model_dir.resolve()) and sha256_file(file) == item['sha256'],
                'base/tokenizer file drift')
    return contract


def load_prepared(directory: Path, contract: Mapping[str, Any], contract_hash: str) -> tuple[list[dict[str, Any]], str]:
    marker = read_json(directory / 'prepared.json')
    path = directory / 'inputs.jsonl'
    require(marker.get('contract_sha256') == contract_hash and marker.get('status') == 'complete'
            and marker.get('input_sha256') == sha256_file(path), 'prepared inputs not frozen')
    rows = read_jsonl(path)
    expected = sum(source['rows'] for source in contract['sources'])
    require(marker.get('rows') == len(rows) == expected
            and len({r['source_id'] for r in rows}) == expected, 'prepared identity/count drift')
    for row in rows:
        require(row['state'] in READY_STATES and text_hash(row['question']) == row['question_sha256'],
                'prepared question/state drift')
        require(not {'query_spec', 'candidate_sql', 'gold_sql', 'training_text'} & row.keys(),
                'prepared inputs contain forbidden offline fields')
        if row['prompt'] is not None:
            require(text_hash(row['prompt']) == row['prompt_sha256'], 'prepared prompt hash drift')
    return rows, marker['input_sha256']


def completion_record(row: Mapping[str, Any], raw: str, tokens: int, eos: bool) -> dict[str, Any]:
    state = row['state']
    if state == 'ready':
        state = 'length_stopped' if not eos else 'generated' if raw.strip() else 'empty_completion'
    return {'source_id': row['source_id'], 'workspace': row['workspace'],
            'prompt_sha256': row['prompt_sha256'], 'state': state,
            'raw_completion': raw, 'raw_completion_sha256': text_hash(raw),
            'generated_tokens': tokens, 'eos_observed': eos}


def load_generation(directory: Path, role: str, rows: list[dict[str, Any]],
                    contract: Mapping[str, Any], contract_hash: str, input_hash: str) -> dict[str, dict[str, Any]]:
    marker = read_json(directory / 'generation.json')
    raw_path = directory / 'raw-completions.jsonl'
    best = read_json(Path(contract['best_freeze']['path']))
    require(marker.get('status') == 'complete' and marker.get('role') == role
            and marker.get('contract_sha256') == contract_hash and marker.get('input_sha256') == input_hash
            and marker.get('raw_sha256') == sha256_file(raw_path)
            and marker.get('decoding') == DECODE, 'generation not completed or matching')
    require(marker.get('adapter_sha256') == (best['adapter_model_sha256'] if role == 'adapter' else None),
            'generation model role/hash drift')
    generated = read_jsonl(raw_path)
    expected = {r['source_id']: r for r in rows}
    require(marker.get('rows') == len(generated) == len(rows), 'generation count drift')
    result = {}
    for record in generated:
        identity = record['source_id']
        require(identity in expected and identity not in result, 'generation missing/duplicate identity')
        row = expected[identity]
        require(record.get('prompt_sha256') == row['prompt_sha256'] and record.get('workspace') == row['workspace']
                and record.get('state') in GENERATION_STATES
                and text_hash(record['raw_completion']) == record.get('raw_completion_sha256'),
                'generation prompt/raw/state drift')
        require(type(record.get('generated_tokens')) is int
                and 0 <= record['generated_tokens'] <= DECODE['max_new_tokens'], 'invalid output length')
        if row['state'] != 'ready':
            require(record['state'] == row['state'] and not record['raw_completion']
                    and record['generated_tokens'] == 0, 'rejected input was generated')
        else:
            require(record['state'] == completion_record(row, record['raw_completion'],
                    record['generated_tokens'], record.get('eos_observed') is True)['state']
                    and record['generated_tokens'] > 0, 'generation EOS/state mismatch')
            if record['state'] == 'length_stopped':
                require(record['generated_tokens'] == DECODE['max_new_tokens'], 'unexpected early stop without EOS')
        result[identity] = record
    require(result.keys() == expected.keys(), 'generation identity mismatch')
    return result


def canonical_frame(frame: pd.DataFrame, contract: Mapping[str, Any]) -> pd.DataFrame:
    """Normalize only Catalog/ResultContract-approved temporal presentation aliases."""
    required = tuple(contract['required_result_columns'])
    if 'time' in required and 'time' not in frame.columns:
        aliases = [name for name in contract.get('result_time_column_aliases', ()) if name in frame.columns]
        if len(aliases) == 1:
            frame = frame.rename(columns={aliases[0]: 'time'})
    require(tuple(frame.columns) == required, 'result column names/order mismatch')
    return frame


def execute_sql(sql: str, workspace: str, plan: Mapping[str, Any], contract: Mapping[str, Any]) -> tuple[dict[str, Any], pd.DataFrame | None]:
    """Offline audited execution: shared policy/probe/validator, workspace reader only."""
    if sql_plan_mismatch(sql, plan):
        return {'state': 'query_plan_guard_rejected'}, None
    profile = WORKSPACES[workspace]
    try:
        decision = SqlPolicy(analyst_limit=1000, admin_limit=1000, workspace=profile).evaluate(sql, 'analyst')
    except PolicyViolation:
        return {'state': 'policy_rejected'}, None
    started = perf_counter()
    connection = psycopg2.connect(host='/tmp', port=35434, database=DATABASES[workspace], user=profile.reader_role)
    try:
        connection.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with connection.cursor() as cursor:
            cursor.execute('SET LOCAL search_path = analytics, pg_catalog')
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute('SET LOCAL statement_timeout = 5000')
            cursor.execute("SELECT current_user, current_setting('transaction_read_only')")
            user, readonly = cursor.fetchone()
            require(user == profile.reader_role and readonly == 'on', 'reader identity/readonly drift')
            probe, count = execution_probe(decision.final_sql, decision.row_limit,
                                           policy_limit_applied=decision.policy_limit_applied)
            cursor.execute(probe)
            columns = [col.name for col in cursor.description]
            values, truncated = cap_probed_rows(cursor.fetchmany(count), decision.row_limit,
                                                policy_limit_applied=decision.policy_limit_applied)
            frame = pd.DataFrame(values, columns=columns)
    except psycopg2.Error as exc:
        if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)) or exc.pgcode == '42501':
            raise MatchingContractError('database connection/reader infrastructure failure') from exc
        return {'state': 'postgres_execution_error', 'sqlstate': exc.pgcode}, None
    finally:
        connection.close()
    validation = ResultValidator(max_rows=1000, display_row_budget=200).validate(
        frame, required_columns=contract['required_result_columns'],
        required_column_aliases=contract.get('required_result_column_aliases', {}),
        metric_columns=contract['metric_result_columns'], time_column=contract.get('result_time_column'),
        time_column_aliases=contract.get('result_time_column_aliases', ()),
        time_bucket_grain=plan.get('time_grain'), requested_start=contract.get('requested_start'),
        requested_end=contract.get('requested_end'), requested_end_exclusive=True,
        exact_columns=True, metric_value_constraints=contract.get('metric_value_constraints', {}),
        execution_truncated=truncated,
    )
    evidence = {'state': 'valid' if validation.safe_to_answer and validation.safe_to_display else 'result_contract_rejected',
                'validation_state': validation.state, 'row_count': len(frame), 'execution_truncated': truncated,
                'reader_role': user, 'transaction_read_only': True,
                'policy_sql_sha256': text_hash(decision.final_sql),
                'elapsed_ms': round((perf_counter() - started) * 1000)}
    if evidence['state'] != 'valid':
        return evidence, None
    try:
        return evidence, canonical_frame(frame, contract)
    except MatchingContractError:
        return {**evidence, 'state': 'result_column_order_mismatch'}, None


def gold_context(source: Mapping[str, Any], workspace: str) -> tuple[str, dict[str, Any], dict[str, Any], Mapping[str, Any]]:
    """Post-generation only: use the original label contract, never fill runtime inputs."""
    spec = source['query_spec']
    profile = WORKSPACES[workspace]
    require(all(spec['workspace'][key] == getattr(profile, key) for key in
                ('catalog_version', 'dataset_version', 'metric_version', 'policy_version')), 'Gold version drift')
    metrics = list(spec['metric_ids'])
    columns = [c for c in spec['required_result_columns'] if c not in metrics] + sorted(metrics)
    sql = source['candidate_sql'] if 'candidate_sql' in source else source['gold_sql']
    catalog = CatalogLoader(profile).load()
    selected = [metric for metric in catalog.metrics if metric.metric_id in metrics]
    require(len(selected) == len(metrics), 'unknown Gold metric')
    bounds = spec['time']
    aliases = list(dict.fromkeys(['time', 'analysis_time', 'date', 'day', 'week', 'month', 'quarter', 'year']
                  + [metric.time_field.rsplit('.', 1)[-1] for metric in selected]))
    contract = {'required_result_columns': columns, 'metric_result_columns': metrics,
                'result_time_column': 'time' if bounds.get('grain') else None,
                'result_time_column_aliases': aliases if bounds.get('grain') else [],
                'required_result_column_aliases': {'time': aliases} if bounds.get('grain') else {},
                'requested_start': bounds.get('start'), 'requested_end': bounds.get('end_exclusive'),
                'metric_value_constraints': {m.metric_id: dict(m.result_value_constraints) for m in selected}}
    plan = {'query_plan_version': 'query-plan-v2', 'time_grain': bounds.get('grain'),
            'dimensions': [spec['dimension']] if spec.get('dimension') else [], 'top_n': spec.get('top_n', {})}
    return reorder_gold_projection(sql, columns), plan, contract, spec


def evaluate_pair(rows: list[dict[str, Any]], base: Mapping[str, Any], adapter: Mapping[str, Any],
                  gold_sources: Mapping[str, Any], *, executor=execute_sql, progress=None) -> dict[str, Any]:
    from scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit import compare_denotations

    records = []
    for row in rows:
        identity = row['source_id']
        candidates = {}
        frames = {}
        for role, generated in (('base', base[identity]), ('adapter', adapter[identity])):
            evidence = {'state': generated['state']}
            if generated['state'] == 'generated':
                try:
                    sql = unwrap_sql_completion(generated['raw_completion'])
                except ValueError:
                    evidence = {'state': 'completion_wrapper_rejected'}
                else:
                    evidence, frame = executor(sql, row['workspace'], row['query_plan'], row['result_contract'])
                    evidence['candidate_sql_sha256'] = text_hash(sql)
                    if frame is not None:
                        frames[role] = frame
            evidence.update(generation_state=generated['state'],
                            generated_tokens=generated.get('generated_tokens', 0),
                            eos_observed=generated.get('eos_observed', False),
                            raw_completion_sha256=generated.get('raw_completion_sha256'))
            candidates[role] = evidence
        source = gold_sources[identity]
        sql, plan, contract, spec = gold_context(source, row['workspace'])
        mismatch = list(compare_source_semantics({'plan': row['query_plan']}, spec)) if row['query_plan'] else ['route_not_admitted']
        mismatch = [reason for reason in mismatch if reason != 'result_column_order_mismatch']
        gold_evidence, gold_frame = executor(sql, row['workspace'], plan, contract)
        require(gold_frame is not None and gold_evidence['state'] == 'valid', 'frozen Gold failed execution/contract')
        for role in candidates:
            candidates[role]['denotation'] = compare_denotations(gold_frame, frames[role]) if role in frames else 'not_eligible'
        records.append({'source_id': identity, 'workspace': row['workspace'], 'input_state': row['state'],
                        'source_semantics_mismatch': mismatch, 'gold_sql_sha256': text_hash(sql),
                        'parent_gold_sql_sha256': text_hash(source['candidate_sql'] if 'candidate_sql' in source else source['gold_sql']),
                        'gold': gold_evidence, **candidates})
        if progress is not None:
            progress({'completed': len(records), 'total': len(rows), 'workspace': row['workspace'],
                      'base_state': candidates['base']['state'], 'adapter_state': candidates['adapter']['state']})
    summary = {}
    for workspace in ('olist', 'thelook', 'all'):
        group = [r for r in records if workspace == 'all' or r['workspace'] == workspace]
        consistent = [r for r in group if not r['source_semantics_mismatch']]
        summary[workspace] = {'total': len(group), 'source_semantics_consistent': len(consistent),
            'input_states': dict(Counter(r['input_state'] for r in group)),
            **{role: {'states': dict(Counter(r[role]['state'] for r in group)),
                      'generation_states': dict(Counter(r[role]['generation_state'] for r in group)),
                      'eos_count': sum(r[role]['eos_observed'] is True for r in group),
                      'denotations': dict(Counter(r[role]['denotation'] for r in group)),
                      'ordered_matches': sum(r[role]['denotation'] == 'ordered_denotation_match' for r in group),
                      'consistent_subset_ordered_matches': sum(r[role]['denotation'] == 'ordered_denotation_match' for r in consistent)}
               for role in ('base', 'adapter')},
            'transitions': dict(Counter(r['base']['denotation'] + ' -> ' + r['adapter']['denotation'] for r in group))}
    return {'summary': summary, 'records': records}
