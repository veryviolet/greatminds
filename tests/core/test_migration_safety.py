import json
import subprocess
import sys
from pathlib import Path

from greatminds.runtime.migration_safety import inspect_execution


def test_quiet_observation_is_read_only_and_not_a_launch_barrier(tmp_path):
    before=list(tmp_path.iterdir())
    result=inspect_execution(tmp_path)
    assert result['observed_quiet'],result
    assert not result['launch_exclusion_verified']
    assert list(tmp_path.iterdir())==before


def test_live_project_process_blocks_observation_without_exposing_command(tmp_path):
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)','private-command-value'],cwd=tmp_path)
    try:
        result=inspect_execution(tmp_path)
        assert {'kind':'project_process_alive','identity':str(process.pid)} in result['holds']
        assert 'private-command-value' not in json.dumps(result)
    finally:
        process.terminate();process.wait(timeout=5)
    assert inspect_execution(tmp_path)['observed_quiet']


def test_detached_process_with_explicit_project_is_detected(tmp_path):
    project=tmp_path/'project';project.mkdir()
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)',
                              '--project-dir',str(project)],cwd=tmp_path)
    try:
        assert not inspect_execution(project)['observed_quiet']
    finally:
        process.terminate();process.wait(timeout=5)


def test_corrupt_registry_and_runtime_state_are_uncertainty_not_quiet(tmp_path):
    runtime=tmp_path/'.greatminds'
    (runtime/'.agent_registry').mkdir(parents=True)
    (runtime/'.agent_registry/developer.json').write_text('{')
    (runtime/'.runtime').mkdir()
    (runtime/'.runtime/state.json').write_text('{')
    kinds={h['kind'] for h in inspect_execution(tmp_path)['holds']}
    assert {'unreadable_registry','unreadable_runtime_state'} <= kinds


def test_live_registered_process_outside_project_is_still_held(tmp_path):
    project=tmp_path/'project';project.mkdir()
    registry=project/'.greatminds/.agent_registry';registry.mkdir(parents=True)
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],cwd=tmp_path)
    try:
        (registry/'developer.json').write_text(json.dumps({'pid':process.pid}))
        assert {'kind':'registered_process_alive','identity':'developer.json'} in inspect_execution(project)['holds']
    finally:
        process.terminate();process.wait(timeout=5)
    assert inspect_execution(project)['observed_quiet']


def test_execution_barrier_excludes_migration_and_releases_on_failure(tmp_path):
    import pytest
    from greatminds.core.errors import GreatMindsError
    from greatminds.runtime.migration_safety import execution_barrier
    with execution_barrier(tmp_path):
        with execution_barrier(tmp_path):
            with pytest.raises(GreatMindsError):
                with execution_barrier(tmp_path,exclusive=True):
                    raise AssertionError('must not enter')
    with execution_barrier(tmp_path,exclusive=True):
        with pytest.raises(GreatMindsError):
            with execution_barrier(tmp_path):
                raise AssertionError('must not enter')
    with execution_barrier(tmp_path):
        pass


def test_exclusive_migration_barrier_prevents_daemon_from_starting(tmp_path):
    import asyncio
    import pytest
    from test_acp_conversations import setup
    from greatminds.core.errors import GreatMindsError
    from greatminds.runtime.daemon import serve
    from greatminds.runtime.migration_safety import execution_barrier
    setup(tmp_path)
    with execution_barrier(tmp_path,exclusive=True):
        with pytest.raises(GreatMindsError,match='execution/migration'):
            asyncio.run(serve(tmp_path,once=True,environment={}))
    assert not (tmp_path/'agent-starts.log').exists()
    asyncio.run(serve(tmp_path,once=True,environment={}))


def test_failed_second_supervisor_does_not_leak_shared_barrier(tmp_path):
    import asyncio
    import pytest
    from test_supervisor import setup, supervisor
    from greatminds.core.errors import GreatMindsError
    from greatminds.runtime.migration_safety import execution_barrier
    store,schema,config,_=setup(tmp_path)
    second=supervisor(tmp_path,store,schema,config)
    async def check():
        async with supervisor(tmp_path,store,schema,config):
            with pytest.raises(GreatMindsError):
                await second.__aenter__()
        with execution_barrier(tmp_path,exclusive=True):
            pass
    asyncio.run(check())
