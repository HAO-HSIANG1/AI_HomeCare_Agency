# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**CareFlow（長照居家照顧 AI 排班系統）** — a Streamlit workbench for a home-care agency's 居督 (care supervisors). It reads one agency Excel workbook (employees, clients, weekly service plan, service codes), builds a recurring monthly/yearly schedule (fixed primary caregiver + backups, CP-SAT joint selection), then supports day-to-day exception handling (caregiver leave, reschedule, delay, new LINE cases, care-need changes), review/export, and manager-level workforce/financial analysis.

Current build: `CAREFLOW_BUILD = '2026.10.18-v19'`. The README (Traditional Chinese) is the authoritative feature/behavior spec; `VALIDATION.md` holds benchmark numbers. Judge feedback and the redefined problem list from the latest demo live in `評審問題重新定義.md`.

The early single-day prototype (Phase 1 scoring → Phase 2 MIP → Phase 3 DiD over `AI_Caregiver_Allocation_Ultimate_Database.xlsx`) still exists in `caregiver_engine.py` / `ai_caregiver_pipeline.py` but is **not** the main product path anymore.

## Running

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py          # main app
python -m pip install -r requirements-dev.txt
python -m pytest -q                     # tests/ (pytest)
python -m unittest test_agency_import.py  # root-level legacy regression tests (test_v14..v18 etc.)
```

Run from the repo root. Default workbook auto-load: `00_DB/居服機構資料_三工作表.xlsx` (users normally upload instead). Deployment: `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`, Streamlit Cloud (`packages.txt`).

Data note: column/sheet names are Traditional Chinese; keep them as-is. Read Excel via pandas or the Read tool — piping workbook contents through the Bash terminal mangles encoding.

## Version guard (important when editing)

`build_info.py` lists `REQUIRED_MODULES`; at startup `app.py` refuses to run unless every listed module contains the exact line `CAREFLOW_BUILD = '<version>'`. When bumping the version, update it in `build_info.py` **and every module in `REQUIRED_MODULES`**. When adding a new core module that app.py depends on, add it to `REQUIRED_MODULES` and give it the stamp.

## Input workbook (`load_agency_workbook` in `caregiver_engine.py`)

One `.xlsx`, sheets matched by alias (exactly one of each required):
- **員工資料** (`員工資料_去識別化`, `Caregiver_Profiles`) — caregivers: qualifications text (`取得資格`), 到職日 (seniority bonus, max 10), workable weekdays/hours, gender, transport mode, 排班期外本月加班時數 …
- **個案資料** (`個案資料_去識別化`, `Client_Profiles`) — clients: location, 歷史首選 (employee ID → primary caregiver, +60), 是否失智 (requires 失智症 20h training), gender requirement …
- **週服務計畫** (`周服務計畫`, `Weekly_Service_Plan`) — recurring weekly slots with service codes (e.g. `BA02+BA20`, `BA08(1)`), minutes, effective/expiry dates, every-other-week cycles; optional 週序列居服員 written back by the app.
- **Service_code** (optional) — 系統代碼 / 必要證照 master (BA08 needs 足部照護 training).
- Plus two optional name-mapping sheets (display only, never enter scheduling).

Legacy column names (`失智症個案(0/1)`, `歷史首選居服員ID`) are still accepted; new columns win. `source_fields.py` normalizes aliases.

## Architecture

**Scheduling core**
- `caregiver_engine.py` (~4.5k lines) — `PipelineConfig` dataclass (all policy defaults incl. wages: 時薪 210, 轉場 46/次, 12h/day, 46h/month overtime, SA10 350), data loading, eligibility helpers, travel-time model (OSRM motorcycle + Google Routes transit, caching), `rank_candidates_by_availability`, `run_monthly_batch_dispatch` (thin wrapper → `monthly_scheduler.run_fixed_period_dispatch`), plus the legacy Phase 1/2/3 functions.
- `monthly_scheduler.py` — fixed-period dispatch: reserve historical owners, allocate new clients, leave substitution, extend to period end with per-date checks.
- `team_scheduler.py` — joint recurring-team planning with **OR-Tools CP-SAT** (`cp_model`), then date-aware extension and repair. `recurring_schedule.py` — recurring-visit identities / representative days.
- `primary_optimizer.py` (+ CLI `optimize_primary.py`, `generate_history.py`) — choose/write back 歷史首選.
- `agency_rules.py` — institution rules shared by auto dispatch, manual changes and payroll (revenue `預估服務營收`, wages, overtime). `schedule_checks.py` — service-hour/rest checks. `eligibility.py` / `care_policy.py` — per-requirement eligibility explanation (cert, weekday, hours, gender, leave…).
- `substitute_rank.py` — **single shared substitute ordering** for leave cover, backups, temporary support, all four temporary events and manual reassignment: hard exclusions first, then familiarity → travel band (≤10/≤20/>20 min) → lower monthly hours → seniority; near overtime cap goes last. Does not use satisfaction (no such data).

**Travel**
- `osrm_client.py` — OSRM motorcycle host (`CAREFLOW_OSRM_MOTORCYCLE_HOST`, default Render `careflow-osrm-motorcycle.onrender.com`; local Docker in `osrm_motorcycle/`), bounded timeouts, never substitutes another mode. `traffic_wake.py` wakes the sleeping Render host in background.
- Google Routes for bus/MRT (`GOOGLE_MAPS_API_KEY` from env or `st.secrets`), queried by actual date/time.
- `persistent_cache.py` — road-leg cache `output_results/cache/road_legs.sqlite3` (30 days, keyed by host + `CAREFLOW_OSRM_MAP_VERSION`).
- `address_geocoder.py` / `build_geo_index.py` — offline New Taipei doorplate geocoder (`data/ntpc_*`).

**Exception handling & workflow**
- `dispatch_workflow.py` — local SQLite `output_results/dispatch_workflow.sqlite3`: tables `records`, `blocks` (leave / unavailable spans), `leave_events`, `pending`, `history` (JSON audit trail), `client_primary`, `workdays`; `render_leave` form (one caregiver × one day per submit).
- `temp_case.py` + `dispatch_message.py` — parse LINE "請問貴單位有人力嗎？" requests and batch-search slots.
- `leave_report.py` — pure pandas: per-leave revenue/payroll delta from before/after day-payroll snapshots (only leaver + substitutes), ledger table, same-day summary, leave-frequency / most-substituted-client patterns. Events live in `dispatch_workflow` table `leave_events` (one row per registration; `remove_block` marks fully revoked events 已撤銷). The before snapshot is taken in `render_leave`'s `on_register` callback; the after snapshot is frozen in app.py right after `payroll_tables` once the cover is confirmed (retaken while travel is still NaN).
- `qualification_recheck.py` — client gains 失智/BA08 → grace period / handover / immediate reassign.
- `pending_clients.py`, `pending_advice.py`, `analysis_bg.py` — unassigned clients (one row per client), whole-client preview, remedies (shift time → change day → change person → split).
- `issue_center.py` — classify conflicts/anomalies (read-only). `demo_cases.py` — finds guaranteed-solvable demo scenarios for the four temporary events.
- `session_store.py` — autosave/restore of the whole work session. `runtime_views.py` — versioned snapshots so filters don't recompute.

**Output & UI**
- `app.py` (~4.9k lines) — Streamlit app, five modules via left nav: 01 資料匯入與排班設定 · 02 智慧排班作業 · 03 衝突與異常處理（含臨時異動：新增／請假／改期／延遲／照顧需求變更）· 04 排班結果與覆核 · 05 人力與效益分析.
- `careflow_ui.py` (presentation helpers, no logic), `calendar_view.py`, `fast_calendar.py`, `dashboard_metrics.py` (pure pandas) + `dashboard_view.py`, `schedule_output.py` (pure views/export, never calls map APIs).
- `schedule_assist.py`, `source_workbook.py`, `workbook_edit.py` — write reviewed owners/weekly clocks back into the original xlsx by editing XML, preserving formatting/formulas. `repair_workbook.py` — minimal workbook edits that raise dispatch success.

## Conventions / invariants

- Hard checks (never relaxed silently): required certs, gender requirement, workable weekdays/hours, leave, travel + rest, 12h/day, monthly overtime cap. Number of team members per client is a preference, not a hard cap.
- Fixed-person rule: 1 primary + up to 2 backups; if none fit a single visit, other qualified caregivers may cover **that visit only** ("臨時支援"), never added to the backup team. Leave cover never overwrites the fixed team.
- Confirmed/notified assignments are protected from automatic re-planning.
- New dispatch-policy constants go into `PipelineConfig` (or the constant block at the top of `substitute_rank.py` for substitute thresholds) — never hardcode them in `app.py`.
- Time strings use `"HH:MM-HH:MM"`; service minutes come from the source slot (only float noise < 1e-6 min tolerated).
- The dashboard now uses native Streamlit components (`dashboard_view.py`), not matplotlib. `setup_chinese_font()` in `app.py` (bundled font in `assets/fonts/`) is currently unused; if matplotlib/seaborn charts are reintroduced, call it *after* any `sns.set_style(...)`, since seaborn resets `font.sans-serif` and drops CJK glyphs.
- UI copy is plain-language Traditional Chinese for supervisors; keep reasons human-readable (e.g. 「服務過 12 次・前一案過來 6 分（順路）」).

## Repo hygiene notes

- `fix_*.py` at the root are one-off string-patch scripts for users who only had an older `app.py`; their changes are already in the current `app.py`. Don't run them, and don't put new logic there.
- `_backup_v15_20261011/`, `careflow-prev/`, `AI_HomeCare_Agency*/`, `*.zip` are old copies/snapshots — not part of the app; exclude them from searches.
- `00_DB/` holds the sample workbook, screenshots and result-report decks; `表單/` holds reference forms.
