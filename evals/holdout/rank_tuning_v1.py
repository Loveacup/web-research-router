#!/usr/bin/env python3
"""One-shot old87 development tuning. Never a formal holdout acceptance run.

Run with PYTHONPATH=. python evals/holdout/rank_tuning_v1.py tune|validation.
Existing artifacts are exclusive-create; validation requires a frozen selection.
The label-blind split must already exist. Network is disabled before imports.
"""
from __future__ import annotations

import socket
NETWORK_ATTEMPTS = []
def deny_network(*args, **kwargs):
    NETWORK_ATTEMPTS.append('blocked')
    raise RuntimeError('rank-tuning-v1 forbids network')
class OfflineSocket(socket.socket):
    def __new__(cls, *args, **kwargs):
        return deny_network(*args, **kwargs)
socket.socket = OfflineSocket
socket.create_connection = socket.getaddrinfo = deny_network

import copy
import hashlib
import inspect
import json
import sys
from pathlib import Path

from evals.holdout import capacity_quality as capacity, score_frozen as frozen
from wrr.core import rubric
from wrr.core.contracts import Candidate, Contribution, PrivacyClass, Kind
from wrr.core.judge import Answer

BASE = Path('evals/holdout/quality-first-87-v2-development')
SPLIT_HASH = 'c42f0b2dbcbfddc0cf4da330c68c9d8088598fbf7e279a52fe2ec55f9e2eea96'
# Fixed before observing tune outcomes; baseline is not a candidate attempt.
CANDIDATES = [
    {'name': 'uncertain_rel', 'key': '-rel', 'low': .35, 'high': .65},
    {'name': 'uncertain_rel50_rrf50', 'key': '-(0.5 * rel + 0.5 * rank_scores[candidate.cid]) if tier == 1 else -rel', 'low': .35, 'high': .65},
    {'name': 'uncertain_rel75_rrf25', 'key': '-(0.75 * rel + 0.25 * rank_scores[candidate.cid]) if tier == 1 else -rel', 'low': .35, 'high': .65},
    {'name': 'uncertain_low25', 'key': '0 if tier == 1 else -rel', 'low': .25, 'high': .65},
    {'name': 'uncertain_high55', 'key': '0 if tier == 1 else -rel', 'low': .35, 'high': .55},
]
BASELINE = {'name': 'current_uncertain_tiered_v1', 'key': '0 if tier == 1 else -rel', 'low': .35, 'high': .65}

def load(path):
    return json.loads(Path(path).read_text())

def save(name, value):
    with (BASE / name).open('x') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def setup():
    frozen.exact(digest(BASE / 'split-v1.json'), SPLIT_HASH, 'immutable split')
    metadata = load(BASE / 'capacity-v3-new-selector.json')
    inputs, hashes = metadata['inputs'], metadata['hashes']
    for key in ('queries', 'protocol', 'preregistration', 'routing_gold', 'content_tasks', 'content_labels'):
        frozen.exact(digest(inputs[key]), hashes[key + '_sha256'], key)
    private = Path(inputs['private_dir'])
    for filename, key in [('index.json', 'coordinator_index_sha256'), ('content-freeze.json', 'content_freeze_sha256')]:
        frozen.exact(digest(private / filename), hashes[key], filename)
    frozen.exact(digest(Path(inputs['run_dir']) / 'manifest.json'), hashes['manifest_sha256'], 'manifest')
    return inputs, hashes, load(private / 'index.json')

def subset_rows(ids, inputs, index):
    """Only selected IDs are mapped or scored; validation labels stay unused in tune."""
    allowed = set(ids)
    entries = {r['id']: r for r in index['queries'] if r['id'] in allowed}
    task_ids = {r['task_id'] for r in entries.values()}
    tasks = {r['task_id']: r for r in frozen.jsonl(inputs['content_tasks']) if r['task_id'] in task_ids}
    labels = {r['task_id']: r for r in frozen.jsonl(inputs['content_labels']) if r['task_id'] in task_ids}
    queries = {q['id']: q for q in frozen.jsonl(inputs['queries']) if q['id'] in allowed}
    rows, records = [], {}
    for sid in ids:
        entry = entries[sid]
        path = Path(inputs['run_dir']) / 'evidence' / (sid + '.json')
        record = load(path) if path.exists() else None
        if record is not None:
            frozen.exact(digest(path), entry['record_sha256'], 'evidence hash')
            frozen.exact(record, entry['record'], 'coordinator evidence')
            frozen.exact(record['sample'], queries[sid], 'query identity')
        records[sid] = record
        trace = (record or {}).get('selection_trace', [])
        raw = trace[-1]['candidates'] if trace else []
        by_cid = frozen.unique(raw, 'cid', 'candidate IDs')
        mapping = frozen.unique(entry['mapping'], 'item_id', 'opaque mapping')
        task, label = tasks[entry['task_id']], labels[entry['task_id']]
        frozen.review(label)
        frozen.exact(task['query'], queries[sid]['query'], 'blind query')
        texts = frozen.unique(task['candidates'], 'item_id', 'blind texts')
        judgments = frozen.unique(label['candidate_labels'], 'item_id', 'blind labels')
        frozen.exact(set(mapping), set(texts), 'text coverage')
        frozen.exact(set(mapping), set(judgments), 'label coverage')
        if record is not None:
            frozen.exact({(m['cid'],m['url']) for m in mapping.values()}, {(c['cid'],c['url']) for c in raw}, 'cid URL coverage')
        candidates = []
        for item_id, m in mapping.items():
            item = judgments[item_id]
            frozen.tristate(item['content_useful']); frozen.rationale(item)
            if record is None:
                continue
            c = by_cid[m['cid']]
            frozen.exact(c['url'],m['url'],'URL binding')
            frozen.exact({k:c[k] for k in ('title','url','snippet')}, {k:texts[item_id][k] for k in ('title','url','snippet')}, 'exact blind text')
            candidates.append({'cid':c['cid'], 'url':c['url'], 'content_useful':item['content_useful'], 'delivered':False, 'rel_killed':False})
        rows.append({'id':sid, 'lang':queries[sid]['lang'], 'status': 'missing_evidence' if record is None else record.get('pipeline_error') or ('ok' if record.get('evidence') else 'missing_evidence'), 'candidates':candidates})
    return rows, records

def policy_function(config, raw):
    if config == BASELINE:
        return rubric.select_policy
    # Same product implementation, changing only ranking scalar/band parameters.
    # No duplicate safety logic, judge prompts, content heuristic or cloud call.
    source = inspect.getsource(rubric.select_policy)
    old = '0 if tier == 1 else -rel'
    assert source.count(old) == 1
    source = source.replace(old, config['key'])
    namespace = dict(vars(rubric))
    scores = [c.get('rrf_score',0) for c in raw]
    lo, hi = min(scores,default=0), max(scores,default=0)
    namespace.update(REL_LOW=config['low'], REL_HIGH=config['high'], rank_scores={c['cid']:((c.get('rrf_score',0)-lo)/(hi-lo) if hi>lo else 0) for c in raw})
    exec(compile(source, '<product-select-ranking-only>', 'exec'), namespace)
    return namespace['select_policy']

def replay(rows, records, config):
    rows = copy.deepcopy(rows)
    selections = {}
    for row in rows:
        record = records[row['id']] or {}
        trace = record.get('selection_trace', [])
        raw = trace[-1]['candidates'] if trace else []
        dropped = set(trace[-1].get('dropped_injection', [])) if trace else set()
        gate = ((record.get('evidence') or {}).get('plan') or {}).get('gate')
        candidates, answers, judged, sensitive = [], {}, set(), set()
        for c in raw:
            cid = c['cid']
            privacy = PrivacyClass(c.get('privacy_class','public'))
            flags = c.get('flags', [])
            protected = privacy != PrivacyClass.PUBLIC or any(f in flags for f in ('unjudged:private','unjudged:sensitive')) or gate in {'local','bibliographic'}
            if protected:
                assert not c.get('judgment')
                sensitive.add(cid)
            judgment = c.get('judgment') or {}
            if judgment and not protected:
                for key in ('rel','inj'):
                    assert frozen.number(judgment.get(key)) and 0 <= judgment[key] <= 1
                    answers[f'{key}.{cid}'] = Answer(type='noul',noul=judgment[key])
                judged.add(cid)
            candidates.append(Candidate(cid=cid,title=c['title'],url=c['url'],snippet=c['snippet'],contributions=[Contribution(**v) for v in c.get('contributions',[])],privacy_class=privacy,source_kind=Kind(c.get('source_kind','web')),rrf_score=c.get('rrf_score',0)))
        outcome = policy_function(config,raw)(candidates,answers,judged,local_injection_ids=dropped,sensitive_ids=sensitive)
        selected = [c.cid for c in outcome.kept[:8]]
        selections[row['id']] = {'top8':selected,'safety_rejected':outcome.dropped_injection}
        for c in row['candidates']:
            c['delivered'] = c['cid'] in selected
    def metrics(subset):
        result = capacity.score_capacity({'queries':subset},8)
        return {k:result[k] for k in ('macro_loss_bounds','macro_precision_bounds','pooled_loss_bounds','pooled_precision_bounds','coverage','metric_status','final_pass')}
    return {'policy':config,'overall':metrics(rows),'by_lang':{lang:metrics([r for r in rows if r['lang']==lang]) for lang in ('en','zh')},'selections':selections}

def dominates(candidate, baseline):
    a,b=candidate['overall'],baseline['overall']
    av=[a['macro_loss_bounds'][s] for s in ('lower','upper')]+[-a['macro_precision_bounds'][s] for s in ('lower','upper')]
    bv=[b['macro_loss_bounds'][s] for s in ('lower','upper')]+[-b['macro_precision_bounds'][s] for s in ('lower','upper')]
    return all(x<=y+1e-12 for x,y in zip(av,bv)) and any(x<y-1e-12 for x,y in zip(av,bv))

def main():
    inputs,hashes,index=setup()
    split=load(BASE/'split-v1.json')
    if sys.argv[1] == 'tune':
        assert not (BASE/'rank-tuning-v1-selection.json').exists()
        rows,records=subset_rows(split['tune'],inputs,index)
        baseline=replay(rows,records,BASELINE)
        save('rank-tuning-v1-tune-baseline.json',baseline)
        attempts=[]
        for i,config in enumerate(CANDIDATES,1):
            attempt=replay(rows,records,config)
            attempt['attempt_n']=i
            attempts.append(attempt)
        eligible=[a for a in attempts if dominates(a,baseline)]
        selected=min(eligible,key=lambda a:(a['overall']['macro_loss_bounds']['upper'],-a['overall']['macro_precision_bounds']['lower'],a['attempt_n'])) if eligible else baseline
        save('rank-tuning-v1-selection.json',{'split_sha256':SPLIT_HASH,'hashes':hashes,'product_sha256':digest('wrr/core/rubric.py'),'harness_sha256':digest(__file__),'attempt_count':len(attempts),'selection_rule':'Require all four macro interval endpoints nonworse and at least one strictly better than baseline; minimize loss upper, maximize precision lower, earliest attempt tie break. Otherwise retain current. Validation adoption uses identical dominance rule. No post-validation tuning.','baseline':baseline,'attempts':attempts,'selected_policy':selected['policy'],'validation_opened':False,'network_attempt_n':len(NETWORK_ATTEMPTS)})
        print(json.dumps({'selected':selected['policy'],'baseline':baseline['overall'],'attempts':[{'name':a['policy']['name'],**a['overall']} for a in attempts]},indent=2))
    elif sys.argv[1] == 'validation':
        selection=load(BASE/'rank-tuning-v1-selection.json')
        frozen.exact(selection['split_sha256'],SPLIT_HASH,'selection split')
        frozen.exact(selection['product_sha256'],digest('wrr/core/rubric.py'),'sealed product')
        frozen.exact(selection['harness_sha256'],digest(__file__),'sealed harness')
        assert not (BASE/'rank-tuning-v1-validation.json').exists()
        # Exclusive seal is created BEFORE opening validation rows or labels.
        save('rank-tuning-v1-validation-opened.json',{'selection_sha256':digest(BASE/'rank-tuning-v1-selection.json'),'split_sha256':SPLIT_HASH,'validation_evaluation_count':1})
        rows,records=subset_rows(split['validation'],inputs,index)
        baseline=replay(rows,records,BASELINE)
        selected=replay(rows,records,selection['selected_policy'])
        result={'split_sha256':SPLIT_HASH,'selection_sha256':digest(BASE/'rank-tuning-v1-selection.json'),'validation_evaluation_count':1,'baseline':baseline,'selected':selected,'adopt':selection['selected_policy']!=BASELINE and dominates(selected,baseline),'network_attempt_n':len(NETWORK_ATTEMPTS)}
        save('rank-tuning-v1-validation.json',result)
        print(json.dumps({k:v if k not in ('baseline','selected') else v['overall'] for k,v in result.items()},indent=2))
    else:
        raise ValueError('expected tune or validation')

if __name__ == '__main__':
    main()
