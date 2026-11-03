"""Measure web read paths on bounded synthetic history; no server or model starts."""
import argparse
import json
from pathlib import Path
import statistics
import math
import tempfile
import time

import yaml

from daemon_idle_benchmark import create_fixture
from greatminds.core.storage import atomic_json
from greatminds.web.service import WebService


def fixture(root, tasks=1000, conversations=20, history=100, turns=100):
    store = create_fixture(root, conversations=conversations, history=history)
    queue = root/'.greatminds/feature_inbox'
    queue.mkdir(exist_ok=True)
    for i in range(tasks):
        task = {'id':f'{i+1:04d}-benchmark','stream':'product','kind':'research','scope':'backend',
                'title':f'Benchmark task {i}', 'reporter':'USER','opened_at':'2026-11-02T00:00:00Z',
                'priority':'normal','blocks':[]}
        (queue/(task['id']+'.yaml')).write_text(yaml.safe_dump(task))
    selected = None
    for path in sorted(store.directory.glob('conversations/*/state.json')):
        doc = json.loads(path.read_text())
        for i in range(turns):
            identity = f'turn-{i}'
            doc['turns'][identity] = {'id':identity,'sequence':i+1,'prompt':'Request '+str(i),
                                     'status':'completed','queued_at':float(i)}
            doc['events'].append({'sequence':i+1,'kind':'text','turn_id':identity,'at':float(i),
                                  'text':('A representative answer with **Markdown**.\n'*50)})
        atomic_json(path,doc)
        selected = doc['id']
    return selected


def measure(call, repeats):
    times=[]
    for _ in range(repeats):
        start=time.perf_counter()
        value=call()
        encoded=json.dumps(value).encode()
        times.append((time.perf_counter()-start)*1000)
    return {'median_ms':round(statistics.median(times),3),
            'p95_ms':round(sorted(times)[math.ceil(len(times)*.95)-1],3),
            'cold_ms':round(times[0],3),
            'response_bytes':len(encoded),'samples_ms':[round(t,3) for t in times]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks',type=int,default=1000)
    parser.add_argument('--conversations',type=int,default=20)
    parser.add_argument('--history',type=int,default=100)
    parser.add_argument('--turns',type=int,default=100)
    parser.add_argument('--repeats',type=int,default=7)
    args=parser.parse_args()
    if not (0<=args.tasks<=10000 and 1<=args.conversations<=100 and 0<=args.history<=2000
            and 1<=args.turns<=256 and 1<=args.repeats<=30):
        parser.error('fixture exceeds supported benchmark bounds')
    with tempfile.TemporaryDirectory(prefix='greatminds-web-benchmark-') as directory:
        root=Path(directory)
        cid=fixture(root,args.tasks,args.conversations,args.history,args.turns)
        service=WebService(root)
        from greatminds.runtime.interactions import ConversationStore
        chat=ConversationStore(service.runtime,cid)
        results={'overview':measure(service.overview,args.repeats),
                 'conversation':measure(lambda:service.conversation(cid),args.repeats),
                 'events_tail':measure(lambda:chat.events(after=args.turns),args.repeats)}
        if hasattr(service,'conversation_updates'):
            results['chat_updates_idle']=measure(lambda:service.conversation_updates(cid,after=args.turns),args.repeats)
        print(json.dumps({'fixture':vars(args),'measurements':results},indent=2))


if __name__=='__main__':
    main()
