"""個案狀態變動的資格重新檢核（第一個情境：案家新增「失智」）。

原本由 A 長期服務的案家，重新評估後確認失智；A 沒有「失智症照顧專長」（20 小時訓練）時，
不只一條路，居督可選三種處理方式：
  grace    寬限期（待受訓）：A 繼續服務到期限，期限後的服務改由 B；追蹤 A 補訓。
  handover 交接過渡：前幾次仍由 A 服務、B 陪同認識案家，之後改由 B 服務。
  now      立即改派：所有受影響服務改由 B。
失智個案怕換人，所以預設推薦「寬限期」；單位或縣市規定必須立即符合時才選「立即改派」。

這裡只放純計算（可單獨測試）；畫面與寫入班表在 app.py。
"""
CAREFLOW_BUILD = '2026.10.18-v19'
from datetime import date, timedelta

import pandas as pd

import caregiver_engine as engine

CERT = '失智症照顧專長'
METHODS = ('grace', 'handover', 'now')
METHOD_LABEL = {
    'grace': '① 寬限期・待受訓（原居服員繼續服務，期限內完成 20 小時訓練）',
    'handover': '② 交接過渡（前幾次由原居服員服務、新居服員陪同，之後改由新居服員）',
    'now': '③ 立即改派（單位或縣市規定必須立即符合時）',
}
METHOD_SHORT = {'grace': '寬限期・待受訓', 'handover': '交接過渡', 'now': '立即改派'}
METHOD_HELP = {
    'grace': '失智長輩對熟悉的照顧者比較穩定，換生面孔容易拒絕服務或出現情緒行為。若單位允許，優先讓原居服員補訓。'
             '期限後仍未取得資格的服務，會先排給新居服員。',
    'handover': '原居服員已確定不補訓、或案家狀況需要更有經驗的人時使用。前幾次讓新居服員陪同熟悉長輩與家屬，降低換人衝擊。',
    'now': '單位內規或縣市契約要求失智個案必須由受訓人員服務、且沒有寬限時使用。',
}
GRACE_DAYS = 60
HANDOVER_VISITS = 2
EMPTY = ('', '未指派', 'None', 'nan', 'NaT')


def _text(value):
    if value is None:
        return ''
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _day(value):
    stamp = pd.to_datetime(value, errors='coerce')
    return '' if pd.isna(stamp) else stamp.strftime('%Y-%m-%d')


def certified(df_cg):
    """有失智症照顧專長的居服員ID。"""
    return {_text(r.get('居服員ID')) for r in df_cg.to_dict('records') if engine.caregiver_has_cert(r, CERT)}


def affected_tasks(client, tasks, current_map, df_cg, since=None):
    """這個案家從 since 起、目前由「沒有失智症照顧專長」的人服務的服務，依日期排序。"""
    ok = certified(df_cg)
    since = _day(since) if since else ''
    rows = tasks.loc[tasks['案家ID'].astype(str) == str(client)]
    out = []
    for r in rows.to_dict('records'):
        tid, day = _text(r.get('任務ID')), _day(r.get('日期'))
        cg = _text(current_map.get(tid))
        if not day or (since and day < since) or cg in EMPTY or cg in ok:
            continue
        out.append({'任務ID': tid, '日期': day, '開始': _text(r.get('時間窗_開始'))[:5],
                    '結束': _text(r.get('時間窗_結束'))[:5], '居服員': cg})
    return sorted(out, key=lambda x: (x['日期'], x['開始']))


def split(method, affected, deadline=None, handover=HANDOVER_VISITS):
    """依處理方式把受影響服務分成：keep（原居服員繼續）、accompany（新居服員陪同）、move（改由新居服員）。"""
    ids = [a['任務ID'] for a in affected]
    if method == 'grace':
        limit = _day(deadline) or '9999-12-31'
        keep = [a['任務ID'] for a in affected if a['日期'] <= limit]
        return {'keep': keep, 'accompany': [], 'move': [t for t in ids if t not in keep]}
    if method == 'handover':
        n = max(0, int(handover))
        return {'keep': ids[:n], 'accompany': ids[:n], 'move': ids[n:]}
    return {'keep': [], 'accompany': [], 'move': ids}


def availability(task_ids, candidate_ids, tasks, effective, df_cg, config, df_cl=None, task_locations=None):
    """每筆服務、每位有證照的候選人能不能接（與改派同一套檢查）；固定人選限制暫時開放，
    因為原居服員資格不符本來就要找人。回傳 {任務ID: [(居服員ID, 理由), ...]}（依代班順序）。"""
    out = {}
    for tid in task_ids:
        with engine._OpenSupport(config, tid):
            try:
                ranked = engine.rank_candidates_by_availability(
                    tid, list(candidate_ids), tasks, effective, df_cg, config,
                    task_locations=task_locations, date_column='日期', df_cl=df_cl)
            except Exception:  # noqa: BLE001
                ranked = []
        out[tid] = [(_text(r['cg_id']), _text(r.get('rank_reason'))) for r in ranked if r.get('available')]
    return out


def covering(avail, need_ids, accompany_ids=()):
    """能接下全部 need_ids（及陪同 accompany_ids）的人，依各筆代班順位的平均名次排序。
    回傳 [{'cg', 'reason', 'avg_rank'}]；need_ids 與 accompany_ids 都空時回傳 []。"""
    need = list(dict.fromkeys([*need_ids, *accompany_ids]))
    if not need:
        return []
    sets = [{cg for cg, _ in avail.get(t, [])} for t in need]
    common = set.intersection(*sets) if sets else set()
    ranked = []
    for cg in common:
        positions = [next(i for i, (c, _) in enumerate(avail[t]) if c == cg) for t in need]
        reason = next((r for c, r in avail[need[0]] if c == cg), '')
        ranked.append({'cg': cg, 'reason': reason, 'avg_rank': sum(positions) / len(positions)})
    return sorted(ranked, key=lambda x: (x['avg_rank'], x['cg']))


def team_after(method, owner, backups, newcomer):
    """處理後的固定人選（主責, 備援）。寬限期：原居服員仍主責，新居服員加入備援；
    交接：新居服員主責，原居服員暫留備援到交接結束；立即改派：新居服員主責，原居服員移出。"""
    backups = [b for b in (backups or []) if b and b not in (owner, newcomer)]
    if method == 'grace':
        return owner, [newcomer, *backups][:2]
    if method == 'handover':
        return newcomer, [owner, *backups][:2]
    return newcomer, backups[:2]


def default_deadline(since):
    start = pd.to_datetime(since, errors='coerce')
    start = date.today() if pd.isna(start) else start.date()
    return start + timedelta(days=GRACE_DAYS)


def followup(method, client, owner, newcomer, parts, deadline=None, created=''):
    """追蹤清單的一筆（04 與臨時異動頁顯示，自動存檔一起保存）。"""
    if method == 'grace':
        todo = f'{owner} 需在 {_day(deadline)} 前完成失智症照顧服務 20 小時訓練，完成後請更新員工資料「取得資格」'
    elif method == 'handover':
        todo = f'前 {len(parts["accompany"])} 次由 {newcomer} 陪同交接，之後由 {newcomer} 服務'
    else:
        todo = f'已全部改由 {newcomer} 服務'
    return {'案家': client, '原居服員': owner, '接手居服員': newcomer, '處理方式': METHOD_SHORT[method],
            '期限': _day(deadline) if method == 'grace' else '', '原居服員續服務': len(parts['keep']),
            '新居服員陪同': len(parts['accompany']), '改由新居服員': len(parts['move']),
            '待辦': todo, '提醒': '請同步把機構 Excel「個案資料」的是否失智改為「是」' + (
                '（寬限期內先不要改，否則重新排班時原居服員會被判定資格不符）' if method == 'grace' else ''),
            '建立時間': created, '狀態': '追蹤中'}
