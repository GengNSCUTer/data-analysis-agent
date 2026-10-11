import json
from unittest.mock import Mock

import pytest

from data_analysis_agent.runtime_candidate_matching import MatchingContractError
from scripts.post_training.evaluation import run_runtime_candidate_matching as matching_cli
from scripts.post_training.launchers import start_runtime_candidate_matching_screen as launcher


def test_generation_argv_uses_shared_parser_and_fixed_gpu_and_contract():
    for role, logical in [('base', 0), ('adapter', 1)]:
        args = matching_cli.parse_args(launcher.stage_argv('generate', role)[4:])
        assert args.command == 'generate' and args.role == role
        assert args.expected_contract_sha256 == launcher.CONTRACT_SHA
        assert args.physical_nvidia_smi_device == launcher.GPU_IDS[logical][0]
        assert args.expected_gpu_uuid == launcher.GPU_IDS[logical][1]
        assert launcher.worker_env(role)['CUDA_VISIBLE_DEVICES'] == str(logical)
    args = matching_cli.parse_args(launcher.stage_argv('evaluate')[4:])
    assert args.base_dir == launcher.RUN_ROOT / 'base'
    assert args.adapter_dir == launcher.RUN_ROOT / 'adapter'


def test_duplicate_launch_fails_before_gpu_or_screen(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'RUN_ROOT', tmp_path)
    (tmp_path / 'launch.json').write_text('{}')
    forbidden = Mock(side_effect=AssertionError('external action on duplicate'))
    monkeypatch.setattr(launcher, 'validate_inputs', forbidden)
    monkeypatch.setattr(launcher.subprocess, 'run', forbidden)
    with pytest.raises(MatchingContractError, match='already launched'):
        launcher.start()
    forbidden.assert_not_called()


@pytest.fixture
def worker_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'RUN_ROOT', tmp_path)
    (tmp_path / 'launch.json').write_text(json.dumps({
        'launcher_sha256': launcher.sha256_file(launcher.Path(launcher.__file__)),
        'contract_sha256': launcher.CONTRACT_SHA, 'input_sha256': 'i'}))
    monkeypatch.setattr(launcher, 'validate_inputs', lambda: ({}, [], 'i'))
    events = []
    monkeypatch.setattr(launcher, 'load_generation', lambda path, role, *args: events.append('verify-' + role))
    def run(argv, log, role):
        events.append('run-' + argv[4])
        if argv[4] == 'evaluate':
            (tmp_path / 'evaluation').mkdir()
            (tmp_path / 'evaluation/safe-report.json').write_text('{}')
        return 0
    monkeypatch.setattr(launcher, 'run_logged', run)
    return tmp_path, events


def test_failed_adapter_never_waits_or_reads_gold(worker_setup, monkeypatch):
    root, events = worker_setup
    monkeypatch.setattr(launcher, 'run_logged', lambda *args: 17)
    monkeypatch.setattr(launcher, 'wait_for_base', lambda: pytest.fail('should not wait on failed Adapter'))
    assert launcher.worker('adapter') == 17
    assert events == []
    assert json.loads((root / 'adapter-exit.json').read_text())['status'] == 'failed'
    assert not (root / 'evaluation-exit.json').exists()


def test_base_worker_only_generates(worker_setup):
    root, events = worker_setup
    assert launcher.worker('base') == 0
    assert events == ['run-generate', 'verify-base']
    assert not (root / 'evaluation-exit.json').exists()


def test_adapter_verifies_both_files_after_base_before_evaluate(worker_setup, monkeypatch):
    root, events = worker_setup
    monkeypatch.setattr(launcher, 'wait_for_base', lambda: events.append('base-complete'))
    assert launcher.worker('adapter') == 0
    assert events == ['run-generate', 'verify-adapter', 'base-complete',
                      'verify-base', 'verify-adapter', 'run-evaluate']
    assert json.loads((root / 'evaluation-exit.json').read_text())['exit_code'] == 0


def test_failed_base_blocks_gold_after_successful_adapter(worker_setup):
    root, events = worker_setup
    (root / 'base-exit.json').write_text(json.dumps({'exit_code': 1, 'status': 'failed'}))
    assert launcher.worker('adapter') == 1
    assert 'run-evaluate' not in events
    assert json.loads((root / 'evaluation-exit.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('status', ['dead', 'timeout'])
def test_dead_or_missing_base_worker_blocks_gold(worker_setup, monkeypatch, status):
    root, events = worker_setup
    if status == 'dead':
        (root / 'base-worker.json').write_text(json.dumps({'pid': 123}))
        monkeypatch.setattr(launcher.os, 'kill', Mock(side_effect=ProcessLookupError))
    else:
        ticks = iter([0, 86401])
        monkeypatch.setattr(launcher.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(MatchingContractError):
        launcher.wait_for_base()
    assert not events


def test_gpu_busy_prevents_launch(monkeypatch):
    uuid = launcher.GPU_IDS[0][1]
    monkeypatch.setattr(launcher.subprocess, 'check_output', lambda *args, **kwargs: f'{uuid}, 5000, 90')
    with pytest.raises(MatchingContractError, match='GPU busy'):
        launcher.check_gpu('base')
