#!/usr/bin/env python3
"""Freeze, prepare, generate and evaluate one shared runtime-candidate v2 pair."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from time import perf_counter

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.post_training_comparison import sha256_file
from data_analysis_agent.runtime_candidate_matching import (
    DECODE, VERSION, WORKSPACES, completion_record, evaluate_pair, load_generation,
    load_prepared, prepare_question, read_json, read_jsonl,
    require, selected_questions, source_asset_refs, validate_contract, write_json,
)
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever

ROOT = Path(__file__).resolve().parents[3]
DATA = Path('/disk2/gengnan/data-analysis-agent-data')
BEST = DATA / 'evals/runtime-candidate-qwen25coder15b-sft-20261011-r1-best-checkpoint-freeze.json'
BEST_SHA = '762a54f373fe9c108721fe13c5a6f469f04af777d68fed8698b96672a6f3b3bf'
RELEASE = DATA / 'evals/runtime-candidate-sql-v2-r1'
FINAL_SOURCES = [
    {'workspace': 'olist', 'format': 'olist', 'rows': 750,
     'path': str(DATA / 'evals/olist-v3-balanced-release-v1.1/sft-release-v1.1/final_evaluation_only/in_domain_test.jsonl'),
     'sha256': '67c1db3f9a3d7a74160306cd1680ea6f3c6be00035d2c95d860bb3d6e1266750',
     'questions_path': str(DATA / 'evals/olist-v3-balanced-release-v1.1/surface-20260914-v3_1/question_variants.json'),
     'questions_sha256': '3603902f707900cbd2e8adce40469f93ad61819d05f3c5f9410e7921645a1b8f',
     'query_specs_path': str(DATA / 'evals/olist-v3-balanced-release-v1.1/structural-20260914-v3_1/query_specs/in_domain_test.jsonl'),
     'query_specs_sha256': '869278808bd1cfca6764eefcc7e2ac84e0ba217a78fbd45204373fe18cc5921a'},
    {'workspace': 'thelook', 'format': 'thelook', 'rows': 750,
     'path': str(DATA / 'evals/thelook-cross-schema-final-test-v3-20260915-r3/cases.jsonl'),
     'sha256': 'bd1195c00dd68a884fc55eb8301184ef1492dd070f01fa26f96293ac13acef7c'},
]
GPU_IDS = {0: (2, 'GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52'),
           1: (3, 'GPU-10863af0-8588-7625-5609-640ba794f64b'),
           2: (0, 'GPU-24fc79b9-0cb5-8a75-17de-188e04e0690b'),
           3: (1, 'GPU-2fce35f7-4f80-b803-74bb-12b9cea0b9f0')}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--scope', choices=('protected_final', 'validation_smoke'), required=True)
    freeze.add_argument('--output', type=Path, required=True)
    freeze.add_argument('--supersedes-contract', type=Path)
    freeze.add_argument('--generation-contract', type=Path,
                        help='Reuse completed preparation/generation markers from this frozen contract.')
    for name in ('prepare', 'generate', 'evaluate'):
        child = commands.add_parser(name)
        child.add_argument('--contract', type=Path, required=True)
        child.add_argument('--expected-contract-sha256', required=True)
        child.add_argument('--output-dir', type=Path, required=True)
        if name != 'prepare':
            child.add_argument('--inputs-dir', type=Path, required=True)
        if name == 'generate':
            child.add_argument('--role', choices=('base', 'adapter'), required=True)
            child.add_argument('--physical-nvidia-smi-device', type=int, required=True)
            child.add_argument('--expected-gpu-uuid', required=True)
        if name == 'evaluate':
            child.add_argument('--base-dir', type=Path, required=True)
            child.add_argument('--adapter-dir', type=Path, required=True)
    return parser.parse_args(argv)


def freeze_contract(scope: str, output: Path, supersedes_contract: Path | None = None,
                    generation_contract: Path | None = None):
    ensure_path_outside_repository(output, ROOT)
    require(sha256_file(BEST) == BEST_SHA, 'preselected best freeze drift')
    best = read_json(BEST)
    manifest = read_json(RELEASE / 'manifest.json')
    require(sha256_file(RELEASE / 'manifest.json') == best['release_manifest_sha256'], 'release drift')
    sources = FINAL_SOURCES if scope == 'protected_final' else [
        {'workspace': workspace, 'format': 'runtime', 'rows': 1,
         'path': str(RELEASE / 'validation.jsonl'), 'sha256': best['validation_data_sha256']}
        for workspace in ('olist', 'thelook')]
    for source in sources:
        for path, expected in source_asset_refs(source):
            require(sha256_file(path) == expected, 'frozen source drift')
    assets = {str(ROOT / file): value for file, value in manifest['runtime_asset_sha256'].items()}
    for file in ('src/data_analysis_agent/runtime_candidate_matching.py',
                 'scripts/post_training/evaluation/run_runtime_candidate_matching.py',
                 'scripts/post_training/evaluation/run_olist_medium_gold_denotation_audit.py'):
        assets[str(ROOT / file)] = sha256_file(ROOT / file)
    value = {'version': VERSION, 'scope': scope, 'frozen_at': datetime.now(timezone.utc).isoformat(),
             'best_freeze': {'path': str(BEST), 'sha256': BEST_SHA},
             'model_dir': manifest['tokenizer']['path'], 'sources': sources,
             'prompt_version': 'runtime-candidate-sql-v2', 'decoding': DECODE,
             'eos_token_id': manifest['tokenizer']['eos_token_id'], 'asset_sha256': assets,
             'sql_repair_enabled': False, 'question_repair_enabled': False,
             'gold_access': 'only_after_both_generation_markers_and_hashes_verified',
             'execution': {'max_rows': 1000, 'preview_rows': 200, 'timeout_ms': 5000,
                           'host': '/tmp', 'port': 35434, 'timezone': 'UTC'},
             'numeric_tolerances': {'absolute': '0.000001', 'relative': '0.000001'}}
    if scope == 'protected_final' and supersedes_contract is None:
        supersedes_contract = DATA / 'evals/runtime-candidate-v2-matching-final-20261011-r1/contract.json'
    if supersedes_contract is not None:
        supersedes_contract = ensure_path_outside_repository(supersedes_contract, ROOT)
        value['supersedes_contract'] = {
            'path': str(supersedes_contract),
            'sha256': sha256_file(supersedes_contract),
            'reason': 'post-generation evaluation handling update; generation artifacts remain unchanged'}
    if generation_contract is not None:
        generation_contract = ensure_path_outside_repository(generation_contract, ROOT)
        generation_sha256 = sha256_file(generation_contract)
        value['generation_contract'] = {'path': str(generation_contract), 'sha256': generation_sha256}
        value['generation_contract_sha256'] = generation_sha256
    output.parent.mkdir(parents=True, exist_ok=True)
    validate_contract_preview(value)
    write_json(output, value)
    return {'status': 'frozen', 'scope': scope, 'contract_sha256': sha256_file(output)}


def validate_contract_preview(contract):
    for path, expected in contract['asset_sha256'].items():
        require(sha256_file(Path(path)) == expected, 'asset drift before freeze')


def verify_before_completion(contract):
    validate_contract_preview(contract)
    for source in contract['sources']:
        for path, expected in source_asset_refs(source):
            require(sha256_file(path) == expected, 'source changed during stage')
    freeze = contract['best_freeze']
    require(sha256_file(Path(freeze['path'])) == freeze['sha256'], 'best freeze changed during stage')
    best = read_json(Path(freeze['path']))
    require(sha256_file(Path(best['adapter_dir']) / 'adapter_model.safetensors')
            == best['adapter_model_sha256'], 'best weights changed during stage')


def fresh_directory(path: Path):
    path = ensure_path_outside_repository(path, ROOT)
    path.mkdir(parents=True, exist_ok=False)
    return path


def prepare(contract, contract_hash, output):
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(contract['model_dir'], local_files_only=True)
    require(tokenizer.eos_token_id == contract['eos_token_id'], 'tokenizer EOS drift')
    rows = []
    for source in contract['sources']:
        retriever = CatalogRetriever(CatalogLoader(WORKSPACES[source['workspace']]).load())
        for row in selected_questions(source, read_jsonl(Path(source['path']))):
            rows.append(prepare_question(row, tokenizer, retriever))
    require(len({r['source_id'] for r in rows}) == len(rows), 'cross-source duplicate identity')
    output = fresh_directory(output)
    path = output / 'inputs.jsonl'
    with path.open('x', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
    from collections import Counter
    marker = {'status': 'complete', 'contract_sha256': contract_hash,
              'input_sha256': sha256_file(path), 'rows': len(rows),
              'states': dict(Counter(r['state'] for r in rows)), 'source_gold_projected': False}
    verify_before_completion(contract)
    write_json(output / 'prepared.json', marker)
    return marker


def gpu_guard(physical, expected_uuid):
    visible = os.environ.get('CUDA_VISIBLE_DEVICES', '')
    require(visible in {'0', '1', '2', '3'} and GPU_IDS[int(visible)] == (physical, expected_uuid),
            'logical/physical GPU mapping mismatch')
    values = subprocess.check_output(['nvidia-smi', '-i', str(physical),
              '--query-gpu=uuid,memory.used,utilization.gpu', '--format=csv,noheader,nounits'], text=True).strip().split(',')
    require(values[0].strip() == expected_uuid and int(values[1]) < 512 and int(values[2]) == 0,
            'GPU busy or UUID mismatch')
    import torch
    require(torch.cuda.is_available() and torch.cuda.device_count() == 1, 'one visible CUDA device required')
    actual = str(torch.cuda.get_device_properties(0).uuid)
    actual = actual if actual.startswith('GPU-') else 'GPU-' + actual
    require(actual == expected_uuid, 'CUDA UUID mismatch')


def generate(contract, contract_hash, inputs, output, role, physical, uuid):
    rows, input_hash = load_prepared(inputs, contract, contract_hash)
    require(not output.exists(), 'generation directory exists; refuse repeat/overwrite')
    gpu_guard(physical, uuid)
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed

    set_seed(DECODE['seed'])
    tokenizer = AutoTokenizer.from_pretrained(contract['model_dir'], local_files_only=True)
    require(tokenizer.eos_token_id == contract['eos_token_id'], 'tokenizer EOS drift')
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(contract['model_dir'], local_files_only=True,
                                               torch_dtype=torch.bfloat16, device_map={'': 0})
    best = read_json(Path(contract['best_freeze']['path']))
    if role == 'adapter':
        model = PeftModel.from_pretrained(model, best['adapter_dir'], is_trainable=False)
    model.eval()
    model.config.use_cache = True
    output = fresh_directory(output)
    path = output / 'raw-completions.jsonl'
    started = perf_counter()
    with path.open('x', encoding='utf-8') as stream:
        for index, row in enumerate(rows, 1):
            raw, count, eos = '', 0, False
            if row['state'] == 'ready':
                encoded = tokenizer(row['prompt'], add_special_tokens=False, return_tensors='pt')
                require(encoded['input_ids'].shape[-1] == row['input_tokens']
                        and row['input_tokens'] <= DECODE['max_input_tokens'], 'prompt token length drift')
                with torch.inference_mode():
                    result = model.generate(**{k: v.to('cuda:0') for k, v in encoded.items()},
                        do_sample=False, num_beams=1, max_new_tokens=DECODE['max_new_tokens'],
                        use_cache=True, pad_token_id=tokenizer.pad_token_id,
                        eos_token_id=tokenizer.eos_token_id)
                tokens = result[0, row['input_tokens']:]
                count = int(tokens.shape[-1])
                eos = count > 0 and int(tokens[-1]) == tokenizer.eos_token_id
                require(eos or count == DECODE['max_new_tokens'], 'generation stopped early without EOS')
                raw = tokenizer.decode(tokens, skip_special_tokens=True)
            record = completion_record(row, raw, count, eos)
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
            stream.flush()
            print(json.dumps({'role': role, 'completed': index, 'total': len(rows),
                              'state': record['state']}), flush=True)
    marker = {'status': 'complete', 'role': role, 'contract_sha256': contract_hash,
              'input_sha256': input_hash, 'raw_sha256': sha256_file(path), 'rows': len(rows),
              'adapter_sha256': best['adapter_model_sha256'] if role == 'adapter' else None,
              'decoding': DECODE, 'elapsed_seconds': round(perf_counter() - started, 3),
              'gpu': {'logical': os.environ['CUDA_VISIBLE_DEVICES'], 'physical': physical, 'uuid': uuid},
              'source_gold_projected': False}
    verify_before_completion(contract)
    write_json(output / 'generation.json', marker)
    return marker


def evaluate(contract, contract_hash, inputs, base_dir, adapter_dir, output):
    rows, input_hash = load_prepared(inputs, contract, contract_hash)
    base = load_generation(base_dir, 'base', rows, contract, contract_hash, input_hash)
    adapter = load_generation(adapter_dir, 'adapter', rows, contract, contract_hash, input_hash)
    # This gate is deliberately before the only post-generation label read.
    require(not output.exists(), 'evaluation output directory already exists')
    identities = {r['source_id'] for r in rows}
    gold = {}
    for source in contract['sources']:
        specs = {}
        if 'query_specs_path' in source:
            for record in read_jsonl(Path(source['query_specs_path'])):
                require(record['seed_id'] not in specs, 'duplicate post-generation Spec identity')
                specs[record['seed_id']] = record['query_spec']
        for row in read_jsonl(Path(source['path'])):
            if source['format'] == 'runtime' and row.get('workspace_key') != source['workspace']:
                continue
            if specs:
                require(row['seed_id'] in specs and row['query_spec_id'] == specs[row['seed_id']]['query_spec_id'],
                        'post-generation source/Spec binding differs')
                row = {**row, 'query_spec': specs[row['seed_id']]}
            key = {'olist': 'seed_id', 'thelook': 'case_id', 'runtime': 'sample_id'}[source['format']]
            identity = source['workspace'] + ':' + row[key]
            if identity in identities:
                require(identity not in gold, 'duplicate post-generation Gold identity')
                gold[identity] = row
    require(gold.keys() == identities, 'post-generation Gold identities differ')
    report = evaluate_pair(rows, base, adapter, gold,
                           progress=lambda value: print(json.dumps(value), flush=True))
    report.update(version=VERSION, scope=contract['scope'], contract_sha256=contract_hash,
                  input_sha256=input_hash,
                  base_raw_sha256=sha256_file(base_dir / 'raw-completions.jsonl'),
                  adapter_raw_sha256=sha256_file(adapter_dir / 'raw-completions.jsonl'),
                  generation_contract_sha256=contract.get('generation_contract_sha256', contract_hash),
                  gold_access_after_pair_frozen=True, raw_question_or_sql_saved=False)
    verify_before_completion(contract)
    output = fresh_directory(output)
    write_json(output / 'safe-report.json', report)
    return report['summary']


def main(argv=None):
    args = parse_args(argv)
    os.environ.setdefault('NCCL_P2P_DISABLE', '1')
    os.environ.setdefault('NCCL_IB_DISABLE', '1')
    if args.command == 'freeze':
        result = freeze_contract(args.scope, args.output, args.supersedes_contract, args.generation_contract)
    else:
        ensure_path_outside_repository(args.contract, ROOT)
        contract = validate_contract(args.contract, args.expected_contract_sha256)
        if args.command == 'prepare':
            result = prepare(contract, args.expected_contract_sha256, args.output_dir)
        elif args.command == 'generate':
            result = generate(contract, args.expected_contract_sha256, args.inputs_dir,
                              args.output_dir, args.role, args.physical_nvidia_smi_device, args.expected_gpu_uuid)
        else:
            result = evaluate(contract, args.expected_contract_sha256, args.inputs_dir,
                              args.base_dir, args.adapter_dir, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
