import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import sys

import pytest

from greatminds.runtime.acp_transport import AcpTransport
from greatminds.runtime.config import parse_execution_config
from greatminds.web.capabilities import discover


def option(identity, category, values, current):
    return NS(id=identity, category=category, type='select', current_value=current,
              options=[NS(value=v, name=v) for v in values])


def test_model_change_refreshes_reasoning_and_applies_it_after_model(tmp_path):
    async def check():
        transport = AcpTransport(['fixture'], workspace=tmp_path)
        transport.callbacks.session_id = 's'
        model = option('model', 'model', ['small', 'large'], 'small')
        initial = NS(modes=None, config_options=[model, option('effort', 'thought_level', ['low'], 'low')])
        changed = [option('model', 'model', ['small', 'large'], 'large'), option('effort', 'thought_level', ['high'], 'high')]
        setter = AsyncMock(return_value=NS(config_options=changed))
        transport.connection = NS(set_config_option=setter)
        result = await transport.configure_session(initial, model='large', reasoning='high')
        assert result is changed
        assert [call.kwargs['config_id'] for call in setter.await_args_list] == ['model', 'effort']
        with pytest.raises(ValueError, match='reasoning is not advertised'):
            await transport.configure_session(initial, model='large', reasoning='low')
    asyncio.run(check())


def config(tmp_path, scenario):
    fixture = Path(__file__).resolve().parents[1] / 'fixtures/acp_server.py'
    return parse_execution_config({'version': 1, 'agents': {'a': {'transport': 'acp',
        'argv': [sys.executable, str(fixture), scenario], 'adapter_version': 'test', 'harness_version': 'test'}},
        'bindings': {'b': {'role': 'TESTER', 'agent': 'a', 'reasoning': 'high'}}}, roles={'TESTER'})


def test_discovery_opens_acp_session_without_prompting(tmp_path):
    doc = config(tmp_path, 'model')
    assert doc.bindings[0].reasoning == 'high'
    result = discover(doc, tmp_path, 'b', 'chosen')
    assert result['model']['current'] == 'chosen'
    assert result['reasoning'] is None
    assert not (tmp_path / 'prompts.log').exists()


def test_missing_capabilities_are_not_invented(tmp_path):
    result = discover(config(tmp_path, 'echo'), tmp_path, 'b')
    assert result == {'model': None, 'reasoning': None, 'modes': []}
