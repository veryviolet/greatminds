"""Explicit, bounded ACP settings discovery without prompting a model."""
import asyncio
import os
import threading

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.acp_transport import AcpTransport


_gate = threading.Lock()


def discover(config, project, binding_id, model=None):
    binding = next((b for b in config.bindings if b.id == binding_id), None)
    if binding is None:
        raise GreatMindsError('Unknown role binding.', exit_code=2)
    agent = next(a for a in config.agents if a.id == binding.agent)
    if not _gate.acquire(blocking=False):
        raise GreatMindsError('ACP discovery is already running. Try again shortly.', exit_code=4)
    async def probe():
        async with AcpTransport(agent.argv, workspace=binding.workspace_path(project),
                                environment=agent.environment_values(os.environ),
                                request_timeout=8, shutdown_timeout=1) as transport:
            async with asyncio.timeout(12):
                session = await transport.open_session()
                options = await transport.configure_session(session, model=model)
                def choices(category):
                    selectors = [o for o in options if o.type == 'select' and o.category == category]
                    # Multiple selectors cannot be expressed by a single value safely.
                    if len(selectors) != 1:
                        return None
                    o = selectors[0]
                    return {'id': o.id, 'current': o.current_value,
                            'options': [{'value': c.value, 'name': c.name}
                                        for group in o.options
                                        for c in (group.options if hasattr(group, 'options') else [group])]}
                return {'model': choices('model'), 'reasoning': choices('thought_level'),
                        'modes': [{'value': m.id, 'name': m.name}
                                  for m in (session.modes.available_modes if session.modes else [])]}
    try:
        return asyncio.run(probe())
    except Exception as exc:
        # Provider errors/stderr may contain credentials. Do not expose them.
        raise GreatMindsError('Cannot discover ACP options. Check the executor installation, '
                              'account login and selected model.', exit_code=2) from exc
    finally:
        _gate.release()
