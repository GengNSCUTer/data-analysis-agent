#!/usr/bin/env python3
"""Start one frozen final pair; Adapter worker waits for Base before Gold."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

from data_analysis_agent.runtime_candidate_matching import (
    load_generation, load_prepared, read_json, require, sha256_file, validate_contract,
    write_json as write_new_json,
)
from scripts.post_training.evaluation.run_runtime_candidate_matching import GPU_IDS

ROOT = Path(__file__).resolve().parents[3]
RUN_ROOT = Path('/disk2/gengnan/data-analysis-agent-data/evals/runtime-candidate-v2-matching-final-20261011-r2')
CONTRACT_SHA = 'c639a1fd71368e7b2e10ce0af199052a72bb69cb36f6b3ec83ea6e0eea46408c'
GPU_PYTHON = '/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python'
CPU_PYTHON = '/disk2/gengnan/conda_envs/data-analysis-agent/bin/python'
MODULE = 'scripts.post_training.evaluation.run_runtime_candidate_matching'
LOGICAL_GPU = {'base': 0, 'adapter': 1}
SESSIONS = {'base': 'daa-runtime-v2-base-20261011-r2', 'adapter': 'daa-runtime-v2-adapter-20261011-r2'}


def write_json(path, payload):
    # Publish status atomically: the waiting peer never reads partially written JSON.
    # link() also keeps exclusive-create semantics for concurrent launch attempts.
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    write_new_json(temporary, payload)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def event(**values):
    print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), **values}, sort_keys=True), flush=True)


def stage_argv(stage: str, role: str | None = None) -> list[str]:
    command = [GPU_PYTHON if stage == 'generate' else CPU_PYTHON, '-u', '-m', MODULE, stage,
               '--contract', str(RUN_ROOT / 'contract.json'), '--expected-contract-sha256', CONTRACT_SHA,
               '--inputs-dir', str(RUN_ROOT / 'inputs'), '--output-dir', str(RUN_ROOT / (role or 'evaluation'))]
    if stage == 'generate':
        physical, uuid = GPU_IDS[LOGICAL_GPU[role]]
        command += ['--role', role, '--physical-nvidia-smi-device', str(physical), '--expected-gpu-uuid', uuid]
    else:
        command += ['--base-dir', str(RUN_ROOT / 'base'), '--adapter-dir', str(RUN_ROOT / 'adapter')]
    return command


def worker_env(role):
    return {**os.environ, 'CUDA_VISIBLE_DEVICES': str(LOGICAL_GPU[role]),
            'PYTHONPATH': str(ROOT / 'src') + ':' + str(ROOT), 'PYTHONUNBUFFERED': '1',
            'TOKENIZERS_PARALLELISM': 'false', 'NCCL_P2P_DISABLE': '1', 'NCCL_IB_DISABLE': '1'}


def check_gpu(role):
    physical, uuid = GPU_IDS[LOGICAL_GPU[role]]
    values = subprocess.check_output(['nvidia-smi', '-i', str(physical),
        '--query-gpu=uuid,memory.used,utilization.gpu', '--format=csv,noheader,nounits'], text=True).strip().split(',')
    require(values[0].strip() == uuid and int(values[1]) < 512 and int(values[2]) == 0,
            'GPU busy or physical UUID mismatch')


def validate_inputs():
    contract = validate_contract(RUN_ROOT / 'contract.json', CONTRACT_SHA)
    require(contract['scope'] == 'protected_final'
            and [(s['workspace'], s['rows']) for s in contract['sources']] == [('olist', 750), ('thelook', 750)],
            'only the fixed full final pair may launch')
    rows, input_hash = load_prepared(RUN_ROOT / 'inputs', contract, CONTRACT_SHA)
    return contract, rows, input_hash


def start():
    require(not (RUN_ROOT / 'launch.json').exists(), 'run already launched; no repeated final generation')
    for name in ('base', 'adapter', 'evaluation', 'base-worker.json', 'adapter-worker.json',
                 'base-exit.json', 'adapter-exit.json', 'evaluation-exit.json'):
        require(not (RUN_ROOT / name).exists(), 'run outputs already exist; refusing overwrite')
    for executable in (GPU_PYTHON, CPU_PYTHON):
        require(Path(executable).is_file(), 'required environment unavailable')
    _, rows, input_hash = validate_inputs()
    screens = subprocess.run(['screen', '-ls'], text=True, capture_output=True).stdout
    for role in ('base', 'adapter'):
        require('.' + SESSIONS[role] not in screens, 'screen session already exists')
        check_gpu(role)
    launch = {'status': 'launching', 'contract_sha256': CONTRACT_SHA, 'input_sha256': input_hash,
              'rows': len(rows), 'created_at': datetime.now(timezone.utc).isoformat(),
              'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'launcher_sha256': sha256_file(Path(__file__)),
              'generation_argv': {role: stage_argv('generate', role) for role in ('base', 'adapter')},
              'evaluation_argv': stage_argv('evaluate'), 'screens': SESSIONS,
              'gpu': {role: {'logical': LOGICAL_GPU[role], 'physical': GPU_IDS[LOGICAL_GPU[role]][0],
                            'uuid': GPU_IDS[LOGICAL_GPU[role]][1]} for role in ('base', 'adapter')},
              'input_states': {workspace: dict(Counter(r['state'] for r in rows if r['workspace'] == workspace))
                               for workspace in ('olist', 'thelook')}}
    # Exclusive create claims the run before any screen; a concurrent start loses here.
    write_json(RUN_ROOT / 'launch.json', launch)
    started = []
    try:
        for role in ('base', 'adapter'):
            subprocess.run(['screen', '-L', '-Logfile', str(RUN_ROOT / (role + '-worker.log')),
                            '-dmS', SESSIONS[role], GPU_PYTHON, '-u', str(Path(__file__)), 'worker', role],
                           cwd=ROOT, env=worker_env(role), check=True)
            started.append(role)
    finally:
        write_json(RUN_ROOT / 'screens-started.json', {'started': started,
                   'status': 'started' if len(started) == 2 else 'launch_failed'})
    event(stage='started', screens=SESSIONS, rows=len(rows), input_states=launch['input_states'])


def run_logged(argv, log, role):
    with log.open('x', encoding='utf-8') as stream:
        return subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT,
                              cwd=ROOT, env=worker_env(role)).returncode


def wait_for_base(timeout=86400):
    deadline = time.monotonic() + timeout
    next_notice = 0.0
    while True:
        status = RUN_ROOT / 'base-exit.json'
        if status.exists():
            result = read_json(status)
            require(result.get('exit_code') == 0 and result.get('status') == 'complete',
                    'Base generation failed; Gold remains blocked')
            return
        now = time.monotonic()
        require(now < deadline, 'Base wait timeout; Gold remains blocked')
        worker = RUN_ROOT / 'base-worker.json'
        if worker.exists():
            try:
                os.kill(read_json(worker)['pid'], 0)
            except ProcessLookupError:
                require(False, 'Base worker exited without completion; Gold remains blocked')
        if now >= next_notice:
            event(stage='waiting_for_base', gold_started=False)
            next_notice = now + 60
        time.sleep(10)


def worker(role):
    # This file also refuses manually starting the same worker twice.
    write_json(RUN_ROOT / (role + '-worker.json'), {'pid': os.getpid(), 'role': role,
               'started_at': datetime.now(timezone.utc).isoformat()})
    code, error_type = 1, None
    try:
        launch = read_json(RUN_ROOT / 'launch.json')
        require(launch['launcher_sha256'] == sha256_file(Path(__file__)), 'launcher changed after start')
        require(launch['contract_sha256'] == CONTRACT_SHA, 'launch contract differs')
        contract, rows, input_hash = validate_inputs()
        require(input_hash == launch['input_sha256'], 'launch input differs')
        event(role=role, stage='generating', gold_started=False)
        code = run_logged(stage_argv('generate', role), RUN_ROOT / (role + '-generation.log'), role)
        if code == 0:
            load_generation(RUN_ROOT / role, role, rows, contract, CONTRACT_SHA, input_hash)
    except Exception as exc:
        code, error_type = 1, type(exc).__name__
    write_json(RUN_ROOT / (role + '-exit.json'), {'status': 'complete' if code == 0 else 'failed',
               'exit_code': code, 'error_type': error_type, 'finished_at': datetime.now(timezone.utc).isoformat()})
    event(role=role, stage='generation_finished', exit_code=code, error_type=error_type)
    if code != 0 or role == 'base':
        return code
    code, error_type = 1, None
    try:
        wait_for_base()
        # Verify BOTH complete files before calling the CLI that opens Gold.
        contract, rows, input_hash = validate_inputs()
        for model_role in ('base', 'adapter'):
            load_generation(RUN_ROOT / model_role, model_role, rows, contract, CONTRACT_SHA, input_hash)
        event(stage='evaluating', pair_verified=True)
        code = run_logged(stage_argv('evaluate'), RUN_ROOT / 'evaluation.log', role)
        if code == 0:
            require((RUN_ROOT / 'evaluation/safe-report.json').is_file(), 'evaluation report missing')
    except Exception as exc:
        code, error_type = 1, type(exc).__name__
    write_json(RUN_ROOT / 'evaluation-exit.json', {'status': 'complete' if code == 0 else 'failed',
               'exit_code': code, 'error_type': error_type, 'finished_at': datetime.now(timezone.utc).isoformat()})
    event(stage='evaluation_finished', exit_code=code, error_type=error_type)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('start')
    child = commands.add_parser('worker')
    child.add_argument('role', choices=('base', 'adapter'))
    args = parser.parse_args(argv)
    if args.command == 'start':
        start()
        return 0
    return worker(args.role)


if __name__ == '__main__':
    raise SystemExit(main())
