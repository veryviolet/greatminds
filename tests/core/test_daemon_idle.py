"""Idle work scales without rereading the entire runtime per conversation."""
import importlib.util
from pathlib import Path


def test_idle_conversations_preserve_history_without_linear_runtime_reads(tmp_path):
    spec = importlib.util.spec_from_file_location('idle_benchmark',
        Path(__file__).resolve().parents[2] / 'tools/daemon_idle_benchmark.py')
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    reads = []
    for count in (1, 20):
        root = tmp_path / str(count)
        root.mkdir()
        store = benchmark.create_fixture(root, conversations=count, history=3)
        benchmark.measure(root, repeats=1)
        before = store.snapshot()
        result = benchmark.measure(root, repeats=1)
        assert store.snapshot() == before
        assert result['run_count'] == 3
        assert result['result_count'] == 0
        reads.append(result['samples'][0]['runtime_reads'])
    assert reads[1] <= reads[0] + 2, reads
