"""請假登錄 → 確認代班 → 請假事件留紀錄（含異動前後快照）→ 04 請假與代班紀錄。不連外部交通服務。"""
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

import caregiver_engine as engine
from dispatch_workflow import WorkflowStore


def _workbook(path):
    employees = pd.DataFrame([{'編號': identifier, '性別': '女', '休息日': '星期日', '取得資格': '失智症照顧服務20小時',
                               '交通工具': '機車', '經度 (WGS84_Lon)': 121.53, '緯度 (WGS84_Lat)': 25.03} for identifier in ['A', 'B']])
    clients = pd.DataFrame([{'內部案號': 'C', '經度 (WGS84_Lon)': 121.54, '緯度 (WGS84_Lat)': 25.04}])
    weekly = pd.DataFrame([{'案號': 'C', '星期': weekday, '開始時間': start, '結束時間': end, '服務分鐘': 60,
                            '頻率': '每週', '生效日': '2026-11-02', '服務項目': 'SA10(1)', '費用類型': '基本',
                            '經度 (WGS84_Lon)': 121.54, '緯度 (WGS84_Lat)': 25.04}
                           for weekday, start, end in [('星期一', '09:00', '10:00'), ('星期三', '15:00', '16:00')]])
    master = pd.DataFrame([{'系統代碼': 'SA10', '服務名稱': '自費服務', '目前給付價格(元)': None,
                            'CareFlow排班分鐘(暫定)': 60, '是否納入CareFlow': '是'}])
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        for name, frame in [('員工資料', employees), ('個案資料', clients), ('週服務計畫', weekly), ('Service_code', master),
                            ('案家姓名對照', pd.DataFrame(columns=['案家ID', '姓名'])),
                            ('居服員姓名對照', pd.DataFrame(columns=['居服員ID', '姓名']))]:
            frame.to_excel(writer, sheet_name=name, index=False)


def test_leave_is_logged_with_cover_and_cost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CAREFLOW_AGENCY_DIR', str(tmp_path))
    monkeypatch.setattr(engine, 'calc_travel_minutes', lambda *a, **k: 5.0)
    monkeypatch.setattr(engine, 'prefetch_osrm_travel_times', lambda *a, **k: None)
    monkeypatch.setattr(engine, 'prefetch_google_transit_pairs', lambda *a, **k: None)
    import monthly_scheduler
    monkeypatch.setattr(monthly_scheduler, 'prefetch_osrm_travel_times', lambda *a, **k: None)
    monkeypatch.setattr(st, 'pyplot', lambda *a, **k: None)
    _workbook(tmp_path / '居服機構資料_三工作表.xlsx')
    app = AppTest.from_file(str(Path(engine.__file__).with_name('app.py')), default_timeout=90).run()
    app.date_input(key='agency_range_start').set_value(date(2026, 11, 1)).run()
    app.radio(key='cf_module').set_value('schedule').run()
    app.button(key='run_full_period').click().run()
    assert not app.exception, [e.message for e in app.exception]
    result = app.session_state['last_result']['phase2']['df_result']
    day = result.loc[result['派單居服員'].eq('A'), '排班日期'].min()
    assert day

    app.session_state['cf_issue_view'] = '臨時異動'
    app.session_state['dynamic_event_type_selector'] = '🚨 居服員臨時請假'
    app.radio(key='cf_module').set_value('issues').run()
    assert not app.exception, [e.message for e in app.exception]
    app.selectbox(key='leave_form_cg').set_value('A')
    app.date_input(key='leave_form_day').set_value(pd.Timestamp(day).date())
    app.radio(key='leave_form_mode').set_value('整天')
    app.selectbox(key='leave_form_reason').set_value('請假')
    next(b for b in app.button if b.label == '登錄不可排時間').click().run()
    assert not app.exception, [e.message for e in app.exception]
    event = WorkflowStore('中化').leave_events()[-1]
    assert event['cg'] == 'A' and event['status'] == '待安排代班' and len(event['affected']) == 1
    assert event['before']['payroll'].get('A')         # 登錄時拍到請假前 A 當天的薪資

    app.button(key='leave_plan_confirm').click().run()
    assert not app.exception, [e.message for e in app.exception]
    event = WorkflowStore('中化').leave_events()[-1]
    assert event['status'] == '已處理'
    assert [(i['代班居服員ID'], i['選擇']) for i in event['items']] == [('B', '推薦')]
    assert event['after']['covered'] == event['affected']   # 確認後重算時拍下異動後快照
    from leave_report import event_cost
    cost = event_cost(event)           # 同一筆 60 分鐘服務由 A 換給 B，兩人當天都沒有其他服務
    assert cost['營收損失'] == 0 and cost['薪資變動'] == 0 and cost['金額狀態'] == '已確認'
    assert event['before']['payroll']['A']['服務基本工資'] == event['after']['payroll']['B']['服務基本工資'] > 0

    app.radio(key='cf_module').set_value('review').run()
    assert not app.exception, [e.message for e in app.exception]
    ledger = next(d.value for d in app.dataframe if '請假人' in d.value.columns)
    assert list(ledger['請假人']) == ['A'] and ledger['已代班'].iloc[0] == 1 and ledger['狀態'].iloc[0] == '已處理'
