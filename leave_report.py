"""請假與代班紀錄：每件請假的營收／成本差額、同日彙總與請假模式統計。

純 pandas，不讀寫資料庫、不呼叫地圖服務。金額來源是 agency_rules.payroll_tables 的
日薪資（daily）與任務財務（task），請假登錄當下拍「異動前」快照，代班確認後拍「異動後」
快照；兩次快照只比較這件請假牽涉的居服員（請假人＋代班人）在當天的薪資。
"""
CAREFLOW_BUILD = '2026.10.18-v19'
import math

import pandas as pd

MONEY = ('服務基本工資', '轉場工資', '加班加給', '預估居服員薪資', '加班時數')


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('nan')


def _day(value):
    stamp = pd.to_datetime(value, errors='coerce')
    return '' if pd.isna(stamp) else stamp.strftime('%Y-%m-%d')


def day_payroll(daily, day):
    """{居服員ID: {服務基本工資, 轉場工資, 加班加給, 預估居服員薪資, 加班時數}}：某一天每位居服員的日薪資。"""
    if daily is None or daily.empty or '日期' not in daily.columns:
        return {}
    rows = daily[pd.to_datetime(daily['日期'], errors='coerce').dt.strftime('%Y-%m-%d') == _day(day)]
    return {str(r['居服員ID']): {k: _num(r.get(k)) for k in MONEY} for r in rows.to_dict('records')}


def task_revenue(tasks_payroll, task_ids):
    """{任務ID: 預估服務營收}，只取指定任務。"""
    if tasks_payroll is None or tasks_payroll.empty:
        return {}
    wanted = {str(t) for t in task_ids}
    rows = tasks_payroll[tasks_payroll['任務ID'].astype(str).isin(wanted)].drop_duplicates('任務ID')
    return {str(t): _num(v) for t, v in zip(rows['任務ID'], rows['預估服務營收'])}


def snapshot(daily, day, tasks_payroll=None, task_ids=(), assigned=None):
    """異動前／後快照：當天全部居服員的日薪資、受影響任務的營收，以及目前有人服務的受影響任務。"""
    shot = {'payroll': day_payroll(daily, day)}
    if tasks_payroll is not None:
        shot['revenue'] = task_revenue(tasks_payroll, task_ids)
    if assigned is not None:
        shot['covered'] = sorted(str(t) for t in task_ids if assigned.get(str(t)))
    return shot


def snapshot_complete(shot):
    """快照裡的薪資都已算出（交通未算完時是 NaN，之後要再拍一次）。"""
    if not shot:
        return False
    return all(math.isfinite(v) for row in shot.get('payroll', {}).values() for v in row.values())


def involved(event):
    """這件請假牽涉的居服員：請假人＋確認代班時選的人。"""
    people = {str(event.get('cg', ''))}
    people |= {str(i['代班居服員ID']) for i in event.get('items', []) if i.get('代班居服員ID')}
    return people - {''}


def _sum(values):
    values = list(values)
    return math.fsum(values) if all(math.isfinite(v) for v in values) else float('nan')


def event_cost(event, live=None):
    """一件請假的差額。live：尚未拍「異動後」快照時，用目前班表的即時快照代替。

    營收損失＝受影響服務中最後沒人服務的營收；薪資變動＝請假人與代班人當天薪資（後－前），
    基本工資因服務換手大致互相抵銷，剩下的主要是代班多出的轉場與加班。
    """
    affected = [str(t) for t in event.get('affected', [])]
    out = {'受影響服務數': len(affected), '已代班': 0, '未派出': 0, '營收損失': 0.0,
           '服務基本工資差': 0.0, '轉場工資差': 0.0, '加班加給差': 0.0, '薪資變動': 0.0, '加班時數差': 0.0,
           '毛利影響': 0.0, '金額狀態': ''}
    if event.get('status') == '已撤銷':
        out['金額狀態'] = '已撤銷，不計差額'
        return out
    before = event.get('before') or {}
    after = event.get('after') or live or {}
    if not before or not after:
        out['金額狀態'] = '尚無快照'
        return out
    covered = set(after.get('covered', []))
    lost = [t for t in affected if t not in covered]
    out['已代班'] = len(affected) - len(lost)
    out['未派出'] = len(lost)
    revenue = before.get('revenue', {})
    out['營收損失'] = _sum(revenue.get(t, float('nan')) for t in lost)
    empty = dict.fromkeys(MONEY, 0.0)
    people = involved(event)
    for key, column in (('服務基本工資差', '服務基本工資'), ('轉場工資差', '轉場工資'), ('加班加給差', '加班加給'),
                        ('薪資變動', '預估居服員薪資'), ('加班時數差', '加班時數')):
        out[key] = (_sum(after['payroll'].get(w, empty)[column] for w in people)
                    - _sum(before['payroll'].get(w, empty)[column] for w in people))
    out['毛利影響'] = -out['營收損失'] - out['薪資變動']
    if not all(math.isfinite(out[k]) for k in ('營收損失', '薪資變動')):
        out['金額狀態'] = '交通或財務資料未算完，金額待確認'
    else:
        out['金額狀態'] = '已確認' if event.get('after') else '即時估算（代班尚未確認）'
    return out


def ledger_frame(events, cg_label=str, client_label=str, live=None):
    """請假與代班紀錄表：一件一列，可直接匯出。live：{事件編號: 即時快照}。"""
    rows = []
    for event in events:
        cost = event_cost(event, (live or {}).get(event['id']))
        items = event.get('items', [])
        subs = sorted({cg_label(i['代班居服員ID']) for i in items if i.get('代班居服員ID')})
        picks = [i.get('選擇', '') for i in items if i.get('代班居服員ID')]
        rows.append({
            '事件編號': event['id'], '收件時間': str(event.get('received', ''))[:16].replace('T', ' '),
            '請假人': cg_label(event['cg']), '日期': event['day'], '時段': event.get('spans_text', ''),
            '原因': event.get('reason', ''), '狀態': event.get('status', ''),
            '受影響服務數': cost['受影響服務數'], '已代班': cost['已代班'], '未派出': cost['未派出'],
            '代班人': '、'.join(subs), '系統推薦': picks.count('推薦'), '居督改選': picks.count('改選'),
            '已通知案家': bool(event.get('notified')),
            '受影響案家': '、'.join(sorted({client_label(c) for c in event.get('clients', [])})),
            '營收損失': cost['營收損失'], '薪資變動': cost['薪資變動'], '轉場工資差': cost['轉場工資差'],
            '加班加給差': cost['加班加給差'], '加班時數差': cost['加班時數差'], '毛利影響': cost['毛利影響'],
            '金額狀態': cost['金額狀態'],
        })
    return pd.DataFrame(rows)


def daily_summary(ledger):
    """同一天多件異動合計：件數、受影響與未派出服務、營收損失與薪資變動。"""
    if ledger is None or ledger.empty:
        return pd.DataFrame()
    live = ledger[ledger['狀態'] != '已撤銷']
    if live.empty:
        return pd.DataFrame()
    grouped = live.groupby('日期', as_index=False).agg(
        請假件數=('事件編號', 'count'), 請假人數=('請假人', 'nunique'), 受影響服務數=('受影響服務數', 'sum'),
        已代班=('已代班', 'sum'), 未派出=('未派出', 'sum'),
        營收損失=('營收損失', lambda s: s.sum(min_count=1) if s.notna().all() else float('nan')),
        薪資變動=('薪資變動', lambda s: s.sum(min_count=1) if s.notna().all() else float('nan')),
        毛利影響=('毛利影響', lambda s: s.sum(min_count=1) if s.notna().all() else float('nan')))
    grouped['派出率'] = (grouped['已代班'] / grouped['受影響服務數'].where(grouped['受影響服務數'] > 0)).mul(100).round(1)
    return grouped.sort_values('日期', ascending=False, ignore_index=True)


def _longest_run(days):
    """連續日期的最長天數。"""
    ordered = sorted({pd.Timestamp(d) for d in days if _day(d)})
    best = run = 0
    for i, day in enumerate(ordered):
        run = run + 1 if i and (day - ordered[i - 1]).days == 1 else 1
        best = max(best, run)
    return best


def caregiver_patterns(events, cg_label=str, today=None, window_days=30, frequent=3, consecutive=3):
    """每位居服員的請假頻率：總件數、請假天數、近 window_days 天件數、最長連續請假天數與提醒。"""
    live = [e for e in events if e.get('status') != '已撤銷' and str(e.get('reason', '')).startswith('請假')]
    if not live:
        return pd.DataFrame()
    today = pd.Timestamp(today or pd.Timestamp.today()).normalize()
    rows = []
    for cg in sorted({e['cg'] for e in live}):
        mine = [e for e in live if e['cg'] == cg]
        days = {e['day'] for e in mine}
        recent = sum(1 for d in days if 0 <= (today - pd.Timestamp(d)).days < window_days)
        longest = _longest_run(days)
        flags = []
        if recent >= frequent:
            flags.append(f'近 {window_days} 天請假 {recent} 天')
        if longest >= consecutive:
            flags.append(f'連續請假 {longest} 天')
        rows.append({'居服員': cg_label(cg), '請假件數': len(mine), '請假天數': len(days), f'近{window_days}天請假天數': recent,
                     '最長連續請假天數': longest, '受影響服務數': sum(len(e.get('affected', [])) for e in mine),
                     '提醒': '、'.join(flags) or '—'})
    return pd.DataFrame(rows).sort_values(['請假天數', '請假件數'], ascending=False, ignore_index=True)


def client_patterns(events, client_label=str):
    """哪些案家最常因請假被換人：被換人次數、不同代班人數。"""
    counts, subs = {}, {}
    for event in events:
        if event.get('status') == '已撤銷':
            continue
        for item in event.get('items', []):
            client = str(item.get('案家ID', ''))
            if not client or not item.get('代班居服員ID'):
                continue
            counts[client] = counts.get(client, 0) + 1
            subs.setdefault(client, set()).add(str(item['代班居服員ID']))
    if not counts:
        return pd.DataFrame()
    return (pd.DataFrame([{'案家': client_label(c), '被換人次數': n, '不同代班人數': len(subs[c])} for c, n in counts.items()])
            .sort_values(['被換人次數', '不同代班人數'], ascending=False, ignore_index=True))
