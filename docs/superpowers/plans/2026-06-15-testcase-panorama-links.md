# 用例库与功能全景关联 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build bidirectional links between testcase libraries/directories and panorama modules, support delete-time cleanup, add testcase change tracking, and expose coverage/risk metrics for a new panorama view.

**Architecture:** Add focused models for links, module test metrics, and testcase change logs. API writes keep data consistent in the same transaction for deletes/updates, while a sync endpoint recalculates stale historical data. Frontend reads precomputed payloads instead of recalculating on page load.

**Tech Stack:** Flask, SQLAlchemy, MySQL auto-migration in `web/app/__init__.py`, Jinja templates, vanilla JavaScript, Python `unittest`.

---

### Task 1: Models And Pure Helpers

**Files:**
- Modify: `web/app/models.py`
- Modify: `web/app/__init__.py`
- Create: `web/app/services/testcase_panorama_links.py`
- Test: `tests/test_testcase_panorama_links.py`

- [ ] Add `TestCasePanoramaLink`, `PanoramaModuleTestMetric`, and `TestCaseChangeLog` models with `to_dict()` methods.
- [ ] Add MySQL `CREATE TABLE IF NOT EXISTS` blocks and indexes.
- [ ] Implement pure helpers:
  - `snapshot_case(case)`
  - `link_case_count(link, cases_query)`
  - `collect_descendant_module_ids(modules, root_id)`
  - `aggregate_module_test_metrics(modules, links)`
- [ ] Unit test descendant aggregation and duplicate-count behavior.
- [ ] Run `python -m unittest tests.test_testcase_panorama_links -v`.

### Task 2: Link API And Sync API

**Files:**
- Modify: `web/app/api/testcases.py`
- Modify: `web/app/api/panorama.py`
- Create: `web/app/api/testcase_panorama_links.py`
- Modify: `web/app/api/__init__.py` if explicit import is needed.
- Test: `tests/test_testcase_panorama_links.py`

- [ ] Add CRUD routes:
  - `GET /api/v1/testcase-panorama-links`
  - `POST /api/v1/testcase-panorama-links`
  - `PUT /api/v1/testcase-panorama-links/{id}`
  - `DELETE /api/v1/testcase-panorama-links/{id}`
- [ ] Add `POST /api/v1/testcase-panorama-links/sync`.
- [ ] Add read routes:
  - `GET /api/v1/testcase-libraries/{id}/panorama-links`
  - `GET /api/v1/panorama/modules/{id}/testcase-links?include_children=1`
  - `GET/POST /api/v1/panorama/modules/{id}/test-metrics`
- [ ] Keep old `related_case_libraries` as fallback only.
- [ ] Test sync dry-run and real cleanup summaries.

### Task 3: Delete-Time Cleanup And Change Logs

**Files:**
- Modify: `web/app/api/testcases.py`
- Modify: `web/app/api/panorama.py`
- Modify: `web/app/services/testcase_panorama_links.py`
- Test: `tests/test_testcase_panorama_links.py`

- [ ] On case create/update/delete/batch create/batch delete/module rename, write `TestCaseChangeLog`.
- [ ] On case delete, hard-delete case-level links and recalculate affected metrics.
- [ ] On library delete, hard-delete all links for that library and recalculate affected metrics.
- [ ] On panorama module recursive delete, hard-delete links and metrics for all deleted module IDs.
- [ ] Add:
  - `GET /api/v1/testcase-libraries/{id}/changes`
  - `GET /api/v1/panorama/modules/{id}/testcase-changes?include_children=1`
- [ ] Test that deleted records disappear from link queries while change logs remain.

### Task 4: Frontend Integration

**Files:**
- Modify: `web/templates/panorama.html`
- Modify: `web/templates/testcases.html`
- Modify: `web/static/js/api.js` only if shared helpers are needed.

- [ ] Add panorama topology metric mode toggle: `模块结构` / `测试覆盖/风险`.
- [ ] In coverage mode, size nodes by `subtree_case_count` and color by `bug_risk_score` or `bug_count`.
- [ ] Add testcase link and metric sections in panorama module detail.
- [ ] Add sync button in panorama detail.
- [ ] Add panorama module badges in testcase library directory view.
- [ ] Add testcase library “最近变更” view with time range, module path, change type, and operator filters.

### Task 5: Skill And Hub Sync

**Files:**
- Modify: `openclaw-agent/skills/testcase-manager/SKILL.md`
- Modify: `openclaw-agent/skills/game-module-panorama/SKILL.md`
- Create/Modify: sync script for testcase-manager if not present.
- Modify: `docs/经验记录.md`

- [ ] Update testcase-manager Skill with link APIs and change query workflow.
- [ ] Update game-module-panorama Skill with test metrics and coverage/risk view workflow.
- [ ] Sync both skills to Hub DB `skills.template_content`.
- [ ] Sync remote disk copies under `/opt/openclaw-web/openclaw-agent/skills/` and `/opt/openclaw-web/hub-store/skills/`.
- [ ] Verify Hub skill content contains `testcase-panorama-links`, `testcase-changes`, `test-metrics`, and `bug_risk_score`.

### Task 6: Verification

**Files:**
- Modify as needed based on lint/test failures.

- [ ] Run `python -m unittest tests.test_testcase_panorama_links -v`.
- [ ] Run targeted syntax checks:
  - `python -m py_compile web/app/models.py web/app/api/testcases.py web/app/api/panorama.py web/app/services/testcase_panorama_links.py`
- [ ] Run `ReadLints` on changed files.
- [ ] Manually verify key pages:
  - `/panorama`
  - `/testcases`
  - relevant API endpoints with existing auth context when available.

