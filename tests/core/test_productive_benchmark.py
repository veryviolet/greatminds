import importlib.util
from pathlib import Path


def test_productive_benchmark_verifies_work_with_unrelated_idle_history():
    path = Path(__file__).resolve().parents[2] / 'tools/productive_benchmark.py'
    spec = importlib.util.spec_from_file_location('productive_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    measured = module.measure(conversations=2, history=3)
    assert measured['run_count'] == measured['applied_results'] == measured['successful_commands'] == 3
    assert measured['independent_test_cases'] == 6 and measured['restart_unchanged'] is True
    assert measured['daemon_runtime_reads'] > 0 and measured['elapsed_seconds'] > 0
    assert [row['role'] for row in measured['roles']] == module.ROLES
    assert all(row['validation']['valid'] is True for row in measured['roles'])
