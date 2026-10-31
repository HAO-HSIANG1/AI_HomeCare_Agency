"""請假紀錄與成本差額：快照比較、同日彙總、請假模式；以及 WorkflowStore 的請假事件表。"""
import math

import pandas as pd

from dispatch_workflow import WorkflowStore
from leave_report import (caregiver_patterns, client_patterns, daily_summary, event_cost, ledger_frame, snapshot,
                          snapshot_complete)

DAY = '2026-10-14'


def _daily(rows):
    return pd.DataFrame([{'居服員ID': cg, '日期': DAY, '服務基本工資': base, '轉場工資': transfer, '加班加給': extra,
                          '預估居服員薪資': base + transfer + extra, '加班時數': hours}
                         for cg, base, transfer, extra, hours in rows])


TASKS = pd.DataFrame([{'任務ID': 'T1', '預估服務營收': 600.0}, {'任務ID': 'T2', '預估服務營收': 400.0}])


def _event(**extra):
    before = snapshot(_daily([('A', 420, 46, 0, 0), ('B', 840, 92, 0, 0), ('Z', 999, 0, 0, 0)]), DAY, TASKS, ['T1', 'T2'])
    return {'id': 1, 'cg': 'A', 'day': DAY, 'reason': '請假', 'status': '已處理', 'affected': ['T1', 'T2'],
            'before': before, **extra}


def test_cost_counts_only_leaver_and_substitutes():
    # A 請假，T1 由 B 代班（多一次轉場、加班 0.5 小時），T2 沒人接；Z 的薪資變動與這件無關。
    after = snapshot(_daily([('B', 1050, 138, 52.5, 0.5), ('Z', 0, 0, 0, 0)]), DAY, TASKS, ['T1', 'T2'], {'T1': 'B'})
    cost = event_cost(_event(after=after, items=[{'任務ID': 'T1', '代班居服員ID': 'B', '選擇': '推薦'}]))
    assert (cost['已代班'], cost['未派出']) == (1, 1)
    assert cost['營收損失'] == 400.0
    assert cost['轉場工資差'] == 0.0          # A 少 46、B 多 46
    assert cost['加班加給差'] == 52.5 and cost['加班時數差'] == 0.5
    assert cost['服務基本工資差'] == 1050 - 420 - 840
    assert cost['毛利影響'] == -400.0 - cost['薪資變動']
    assert cost['金額狀態'] == '已確認'


def test_unknown_travel_and_revoked():
    after = snapshot(_daily([('B', 1050, 138, float('nan'), float('nan'))]), DAY, TASKS, ['T1', 'T2'], {'T1': 'B', 'T2': 'B'})
    assert not snapshot_complete(after) and not snapshot_complete(None)
    cost = event_cost(_event(after=after, items=[{'代班居服員ID': 'B'}]))
    assert math.isnan(cost['薪資變動']) and cost['營收損失'] == 0.0 and '待確認' in cost['金額狀態']
    assert event_cost(_event(status='已撤銷'))['金額狀態'].startswith('已撤銷')


def test_live_snapshot_before_confirmation():
    live = snapshot(_daily([('B', 840, 92, 0, 0)]), DAY, TASKS, ['T1', 'T2'], {})
    cost = event_cost(_event(status='待安排代班'), live)
    assert cost['營收損失'] == 1000.0 and cost['金額狀態'].startswith('即時估算')


def test_ledger_daily_summary_and_patterns():
    after = snapshot(_daily([('B', 1050, 138, 0, 0)]), DAY, TASKS, ['T1', 'T2'], {'T1': 'B', 'T2': 'B'})
    items = [{'任務ID': 'T1', '案家ID': 'C1', '代班居服員ID': 'B', '選擇': '推薦'},
             {'任務ID': 'T2', '案家ID': 'C1', '代班居服員ID': 'B', '選擇': '改選'}]
    events = [_event(after=after, items=items, clients=['C1']),
              {**_event(), 'id': 2, 'cg': 'A', 'day': '2026-10-15', 'status': '待安排代班'},
              {**_event(), 'id': 3, 'cg': 'A', 'day': '2026-10-16', 'status': '已撤銷'}]
    ledger = ledger_frame(events)
    assert list(ledger['事件編號']) == [1, 2, 3]
    assert ledger.loc[0, '系統推薦'] == 1 and ledger.loc[0, '居督改選'] == 1 and ledger.loc[0, '代班人'] == 'B'
    summary = daily_summary(ledger)
    assert set(summary['日期']) == {DAY, '2026-10-15'}          # 已撤銷不列入
    assert summary.set_index('日期').loc[DAY, '派出率'] == 100.0
    patterns = caregiver_patterns(events, today='2026-10-20', consecutive=2)
    assert patterns.loc[0, '請假天數'] == 2 and '連續請假 2 天' in patterns.loc[0, '提醒']
    clients = client_patterns(events)
    assert clients.loc[0, '被換人次數'] == 2 and clients.loc[0, '不同代班人數'] == 1


def test_store_leave_events_and_revoke(tmp_path):
    store = WorkflowStore('inst', tmp_path / 'workflow.sqlite3')
    ids = store.add_blocks('A', DAY, [('09:00', '10:00'), ('14:00', '15:00')], '請假')
    assert len(ids) == 2 and store.add_blocks('A', DAY, [('09:00', '10:00')], '請假') == ids[:1]
    eid = store.add_leave_event('A', DAY, {'reason': '請假', 'blocks': ids, 'status': '待安排代班'})
    store.update_leave_event(eid, notified=True)
    event = store.leave_events()[0]
    assert event['id'] == eid and event['notified'] and event['received']
    store.remove_block(ids[0])
    assert store.leave_events()[0]['status'] == '待安排代班'     # 還有一段沒撤銷
    store.remove_block(ids[1])
    assert store.leave_events()[0]['status'] == '已撤銷'
    assert WorkflowStore('other', tmp_path / 'workflow.sqlite3').leave_events() == []
