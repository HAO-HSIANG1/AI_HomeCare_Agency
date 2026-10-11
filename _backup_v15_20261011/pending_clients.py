"""One pending row per client and a read-only whole-client assignment preview."""
CAREFLOW_BUILD = '2026.10.11-v15'
from dataclasses import replace
import hashlib
from datetime import datetime
import pandas as pd
from agency_rules import clean, leave_overlap
from recurring_schedule import visit_pattern


def pattern_token(task):
    return hashlib.sha256(repr(visit_pattern(task)).encode()).hexdigest()[:16]


def pending_client_summary(tasks, assignments, diagnostics=None, owners=None):
    """Count unique clients separately from their actual pending visits."""
    columns = ['案家ID','固定居服員ID','未派服務數','全部服務數','已派服務數',
               '首次未派日期','最後未派日期','每週待處理時段','未派原因']
    valid = assignments.copy()
    if {'任務ID','派單居服員'}.issubset(valid.columns):
        valid = valid.loc[valid['派單居服員'].map(clean).isin(['','未指派','None','nan']).eq(False)]
        assigned = set(valid['任務ID'].astype(str))
    else:
        assigned = set()
    unique = tasks.drop_duplicates('任務ID').copy()
    unique['_pending'] = ~unique['任務ID'].astype(str).isin(assigned)
    diag = diagnostics.to_dict('records') if isinstance(diagnostics,pd.DataFrame) else diagnostics or []
    diag = {str(row.get('任務ID')):row for row in diag}
    rows = []
    for client,frame in unique.groupby('案家ID',sort=True):
        pending = frame.loc[frame['_pending']]
        if pending.empty:
            continue
        slots = sorted({f"{pd.Timestamp(t['日期']).day_name() if not clean(t.get('星期')) else t['星期']} {t['時間窗_開始']}～{t['時間窗_結束']}"
                        for t in pending.to_dict('records')})
        reasons = sorted({clean(diag.get(str(t),{}).get('問題分類')) or
                          clean(diag.get(str(t),{}).get('診斷')) or '待核對目前班表'
                          for t in pending['任務ID']})
        rows.append(dict(zip(columns,[str(client),(owners or {}).get(str(client),''),len(pending),len(frame),
            len(frame)-len(pending),str(pending['日期'].min())[:10],str(pending['日期'].max())[:10],
            '；'.join(slots),'；'.join(reasons)])))
    return pd.DataFrame(rows,columns=columns).sort_values('未派服務數',ascending=False,kind='stable').reset_index(drop=True)


def client_patterns(tasks, client):
    rows = []
    seen = set()
    for task in tasks.loc[tasks['案家ID'].astype(str).eq(str(client))].to_dict('records'):
        token = pattern_token(task)
        if token in seen:
            continue
        seen.add(token)
        rows.append({'模式ID':token,'來源列':task.get('來源列',''),'星期':task.get('星期',''),
                     '開始時間':task['時間窗_開始'],'結束時間':task['時間窗_結束'],
                     '服務分鐘':task['服務歷時(分鐘)']})
    return pd.DataFrame(rows)


def preview_client_assignment(tasks, assignments, caregivers, clients, config, client, worker, patterns=None, pattern_workers=None, task_workers=None):
    """Check every date atomically; caller decides whether to apply the preview.

    The selected worker becomes the client owner across all weekdays. Only
    overlapping actual leave permits retaining/selecting a qualified backup.
    pattern_workers（模式ID → 居服員）可把不同星期／時段交給不同人：被指定的人與主責一起成為
    該案的固定人選團隊（主責＋備援），每一筆仍逐項檢查資格、交通、休息與工時。
    """
    import caregiver_engine as engine
    client,worker = str(client),str(worker)
    pattern_workers = {str(k):str(v) for k,v in (pattern_workers or {}).items() if str(v) and str(v)!='nan'}
    proposed = engine.merge_task_client_tables(tasks,clients).copy()
    proposed['日期'] = pd.to_datetime(proposed['日期']).dt.strftime('%Y-%m-%d')
    people = {str(p['居服員ID']):p for p in caregivers.to_dict('records')}
    task_workers = {str(k):str(v) for k,v in (task_workers or {}).items() if str(v) and str(v)!='nan'}
    team = [worker] + sorted({w for w in [*pattern_workers.values(),*task_workers.values()] if w!=worker})
    if any(w not in people for w in team):
        raise ValueError('找不到選取的居服員。')
    mask = proposed['案家ID'].astype(str).eq(client)
    if not mask.any():
        raise ValueError('找不到案家服務。')
    edits = {str(r['模式ID']):r for r in patterns.to_dict('records')} if patterns is not None else {}
    for index,task in proposed.loc[mask].iterrows():
        edit = edits.get(pattern_token(task))
        if not edit:
            continue
        try:
            start,end = [datetime.strptime(str(edit[k]).strip(),'%H:%M') for k in ('開始時間','結束時間')]
        except ValueError as exc:
            raise ValueError('請使用 HH:MM 時間格式。') from exc
        if end<=start or abs((end-start).total_seconds()/60-float(task['服務歷時(分鐘)']))>1e-6:
            raise ValueError('整案調整須維持每筆原服務分鐘，且結束時間晚於開始。')
        proposed.loc[index,['時間窗_開始','時間窗_結束']] = [start.strftime('%H:%M'),end.strftime('%H:%M')]
    selected = proposed.loc[mask].sort_values(['日期','時間窗_開始','任務ID'])
    case_ids = set(selected['任務ID'])
    remaining = assignments.loc[~assignments['任務ID'].isin(case_ids)].copy()
    if '派單居服員' in remaining:
        remaining = remaining.loc[~remaining['派單居服員'].map(clean).isin(['','未指派','None','nan'])]
    trial = replace(config,client_primary_map={**config.client_primary_map,client:worker},
                    client_backup_map={**getattr(config,'client_backup_map',{}),client:tuple(team[1:])},
                    primary_profiles=people,work_day_minutes=dict(config.work_day_minutes),dispatch_cache={})
    days = {day:frame.copy() for day,frame in proposed.groupby('日期',sort=True)}
    assigned = {day:remaining.loc[remaining['任務ID'].isin(frame['任務ID'])].copy() for day,frame in days.items()}
    trial.dispatch_cache={'caregivers':caregivers,'people':people,'busy':engine._build_cg_busy_blocks(caregivers),
        'days':{id(f):{'frame':f,'ids':set(f['任務ID']),'lookup':f.set_index('任務ID').to_dict('index')} for f in days.values()}}
    prepared = set()
    accepted,failed,leaves = [],[],[]
    def evaluate(task,candidate):
        month = task['日期'][:7]
        key = candidate,month
        if key not in prepared:
            current = pd.concat([remaining,pd.DataFrame(accepted,columns=['任務ID','派單居服員'])],ignore_index=True)
            trial.work_day_minutes = engine._effective_month_ledger('',candidate,pd.Timestamp(task['日期']),
                proposed,current,caregivers,trial)
            prepared.add(key)
        return engine._evaluate_reassignment(task['任務ID'],candidate,days[task['日期']],
                                            assigned[task['日期']],caregivers,trial)
    def add(task,candidate,checked):
        row={'任務ID':task['任務ID'],'派單居服員':candidate}
        accepted.append(row)
        assigned[task['日期']] = pd.concat([assigned[task['日期']],pd.DataFrame([row])],ignore_index=True)
        trial.work_day_minutes[(candidate,task['日期'])]=checked['working_minutes']
    for _,task in selected.iterrows():
        owner = task_workers.get(str(task['任務ID'])) or pattern_workers.get(pattern_token(task),worker)
        if leave_overlap(task,people[owner]):
            leaves.append((task,owner))
            continue
        checked = evaluate(task,owner)
        if checked['available']:
            add(task,owner,checked)
        else:
            failed.append({'任務ID':task['任務ID'],'日期':task['日期'],'原因':checked['detail']})
    old = dict(zip(assignments.get('任務ID',[]),assignments.get('派單居服員',[])))
    for task,owner in leaves:
        previous = clean(old.get(task['任務ID']))
        candidates = ([previous] if previous in people and previous!=owner else [])
        candidates += [p for p in people if p!=owner and p not in candidates]
        reasons=[]
        for candidate in candidates:
            checked=evaluate(task,candidate)
            if checked['available']:
                add(task,candidate,checked)
                break
            reasons.append(f'{candidate}：{checked["detail"]}')
        else:
            failed.append({'任務ID':task['任務ID'],'日期':task['日期'],'原因':'請假代班缺人；'+'；'.join(reasons)})
    return {'tasks':proposed,'accepted':pd.DataFrame(accepted,columns=['任務ID','派單居服員']),
            'failed':pd.DataFrame(failed,columns=['任務ID','日期','原因']),
            'client':client,'worker':worker,'team':tuple(team[1:]),'total':len(selected),'primary_map':trial.client_primary_map,
            'pattern_workers':pattern_workers,'task_workers':task_workers,
            'ledger':dict(trial.work_day_minutes)}


def suggest_team(tasks, accepted_by_worker, max_team=3, preferred=None):
    """沒有人能一人接下整案時，依「每個固定時段（模式）整組交給同一人」做貪婪組合。

    tasks：該案全部服務（含 任務ID 與可算 pattern_token 的欄位）。
    accepted_by_worker：{居服員: 單獨試算時可排進的任務ID集合}。
    回傳 {'pattern_workers': {模式ID: 居服員}, 'main': 主責, 'covered': 可排次數, 'total': 總次數, 'team': [...]}；
    組合只是建議，實際可行與否要再用 preview_client_assignment 驗證。
    """
    rows = [(str(t['任務ID']),pattern_token(t)) for t in tasks.to_dict('records')]
    patterns = {}
    for tid,token in rows:
        patterns.setdefault(token,[]).append(tid)
    remaining = dict(patterns)
    chosen,team = {},[]
    while remaining and len(team) < max_team:
        best,best_key = None,(0,0)
        for worker,ids in accepted_by_worker.items():
            if worker in team:
                continue
            ids = {str(i) for i in ids}
            whole = [tok for tok,tids in remaining.items() if all(t in ids for t in tids)]
            visits = sum(len(remaining[tok]) for tok in whole)
            partial = sum(len(ids.intersection(tids)) for tids in remaining.values())
            key = (visits,partial) if visits else (0,partial)
            if worker == preferred:
                key = (key[0],key[1]+0.5)
            if key > best_key:
                best,best_key = worker,key
        if best is None:
            break
        ids = {str(i) for i in accepted_by_worker[best]}
        take = [tok for tok,tids in remaining.items() if all(t in ids for t in tids)]
        if not take:   # 沒有完整涵蓋的固定時段：把能排最多的那幾個時段先給他
            take = [tok for tok,tids in remaining.items() if ids.intersection(tids)]
        for tok in take:
            chosen[tok] = best
            remaining.pop(tok)
        team.append(best)
    covered = sum(len({str(i) for i in accepted_by_worker[chosen[tok]]}.intersection(patterns[tok])) for tok in chosen)
    load = {}
    for tok,worker in chosen.items():
        load[worker] = load.get(worker,0) + len(patterns[tok])
    main = max(load,key=load.get) if load else (preferred or '')
    return {'pattern_workers':chosen,'main':main,'covered':covered,'total':len(rows),'team':team,
            'uncovered_patterns':list(remaining)}


def plan_team(tasks, assignments, caregivers, clients, config, client, patterns, accepted_by_worker, preferred=None,
              max_team=3, max_extra=1):
    """建議並驗證「多人分派」：先把固定時段整組分給最適合的人，剩下排不進的個別日期再逐筆補人。

    回傳 {'main','pattern_workers','task_workers','team','accepted','failed','total','preview'}；
    preview 是最後一次真實驗證（逐筆檢查資格、交通、休息、工時）的結果。
    """
    client = str(client)
    frame = tasks.loc[tasks['案家ID'].astype(str).eq(client)]
    plan = suggest_team(frame, accepted_by_worker, max_team=max_team, preferred=preferred)
    main = plan['main'] or (preferred or '')
    if not plan['pattern_workers']:
        return {**plan,'main':main,'task_workers':{},'accepted':0,'failed':len(frame),'total':len(frame),'preview':None}
    task_workers,preview = {},None
    for _ in range(3):
        preview = preview_client_assignment(tasks,assignments,caregivers,clients,config,client,main,patterns,
                                            plan['pattern_workers'],task_workers)
        failed = [str(t) for t in preview['failed']['任務ID']]
        if not failed:
            break
        members = set(plan['pattern_workers'].values()) | set(task_workers.values()) | {main}
        changed = False
        for tid in failed:
            if task_workers.get(tid) and tid not in set(preview['failed']['任務ID'].astype(str)):
                continue
            options = [(w in members, len(ids)) for w,ids in accepted_by_worker.items() if tid in {str(i) for i in ids}]
            ranked = sorted(((w in members, len(ids), w) for w,ids in accepted_by_worker.items()
                             if tid in {str(i) for i in ids} and w != task_workers.get(tid)), reverse=True)
            if not ranked:
                continue
            new_members = members | {ranked[0][2]}
            if len(new_members) > max_team + max_extra and ranked[0][2] not in members:
                inside = [r for r in ranked if r[0]]
                if not inside:
                    continue
                ranked = inside
            task_workers[tid] = ranked[0][2]
            members.add(ranked[0][2])
            changed = True
        if not changed:
            break
    return {**plan,'main':main,'task_workers':task_workers,'accepted':len(preview['accepted']),
            'failed':len(preview['failed']),'total':preview['total'],'preview':preview}
