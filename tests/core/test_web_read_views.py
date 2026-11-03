import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from greatminds.core.storage import atomic_json
from greatminds.runtime.interactions import ConversationStore
from greatminds.web.service import WebService
from greatminds.web.snapshots import ReadCache
from test_acp_conversations import setup


def test_overview_does_not_run_domain_diagnostics_and_invalidates_files(tmp_path):
    chat = setup(tmp_path)
    service = WebService(tmp_path)
    path = service.runtime/'feature_inbox/0001-task.yaml'
    path.parent.mkdir()
    path.write_text('title: First\n')
    with patch('greatminds.runtime.observation.snapshot', side_effect=AssertionError('heavy diagnostics')):
        first = service.overview()
        assert first['tasks'][0]['title'] == 'First'
        with patch('greatminds.web.snapshots.yaml.load', side_effect=AssertionError('unchanged YAML parsed')):
            assert service.overview()['tasks'] == first['tasks']
        path.write_text('title: Other\n')  # Same size, changed identity must invalidate.
        assert service.overview()['tasks'][0]['title'] == 'Other'
        destination = service.runtime/'feature_plan'/path.name
        destination.parent.mkdir()
        path.rename(destination)
        assert service.overview()['tasks'][0]['queue'] == 'feature_plan'
        destination.unlink()
        assert service.overview()['tasks'] == []
        chat.enqueue('new request', request_id='new')
        assert service.overview()['conversations'][0]['turn_count'] == 1


def test_chat_window_and_incremental_updates_preserve_history_without_replay(tmp_path):
    chat = setup(tmp_path)
    doc = chat.snapshot()
    for i in range(70):
        identity = f'turn-{i}'
        doc['turns'][identity] = {'id':identity,'sequence':i+1,'status':'completed','prompt':str(i)}
        doc['events'].append({'sequence':i+1,'kind':'text','turn_id':identity,'text':'answer '+str(i)})
    atomic_json(chat.path,doc)
    service = WebService(tmp_path)
    latest = service.conversation_updates(chat.id)
    assert len(latest['turns']) == 50 and latest['before'] == 21 and latest['has_older']
    assert latest['outputs']['turn-69'] == 'answer 69'
    idle = service.conversation_updates(chat.id, after=latest['cursor'])
    assert idle['turns'] == {} and idle['events'] == [] and 'outputs' not in idle
    older = service.conversation_updates(chat.id, before=latest['before'])
    assert len(older['turns']) == 20 and not older['has_older']
    chat.enqueue('next question',request_id='next')
    changed = service.conversation_updates(chat.id,after=latest['cursor'])
    assert list(changed['turns']) == ['next']
    assert changed['events'][0]['kind'] == 'queued'
    assert changed['cursor'] == 71
    with pytest.raises(Exception,match='ahead'):
        service.conversation_updates(chat.id,after=10000)
    doc = chat.snapshot()
    doc['turns']['turn-0']['status'] = 'running'
    atomic_json(chat.path, doc)
    refreshed = service.conversation_updates(chat.id)
    assert refreshed['outputs']['turn-0'] == 'answer 0'
    assert refreshed['before'] == 22  # Active turn must not skip older pages.


def test_read_cache_does_not_keep_a_value_read_during_replacement(tmp_path):
    path = tmp_path/'value';path.write_text('old')
    cache=ReadCache(max_entries=2)
    def raced():
        old=path.read_text();path.write_text('new');return old
    assert cache.read('key',[path],raced) == 'old'
    assert cache.read('key',[path],path.read_text) == 'new'
    for i in range(10):
        cache.read(i,[path],path.read_text)
    assert len(cache.entries) == 2


def test_run_history_is_bounded_and_can_be_paged(tmp_path):
    chat = setup(tmp_path)
    service = WebService(tmp_path)
    with service.store._transaction() as state:
        for i in range(260):
            state['runs'][str(i)] = {'id':str(i),'sequence':i+1,'state':'completed','binding_id':'chat',
                'task_id':'chat-'+chat.id,'created_at':i,'updated_at':i,'config_sha256':'fixture',
                'role':'ARCHITECT-PLANNER','agent_id':'fixture','task_revision':'fixture'}
        state['runs']['0']['state'] = 'running'
    first = service.overview()
    assert first['run_count'] == 260 and first['run_totals']['completed'] == 259
    assert len(first['runs']) == 101  # Old active run must not skip history pages.
    second = service.run_page(before=first['runs_before'])
    third = service.run_page(before=second['runs'][-1]['sequence'])
    assert len(second['runs']) == 100 and second['has_more']
    assert len(third['runs']) == 60 and not third['has_more']
    assert len({r['id'] for r in first['runs']+second['runs']+third['runs']}) == 260
