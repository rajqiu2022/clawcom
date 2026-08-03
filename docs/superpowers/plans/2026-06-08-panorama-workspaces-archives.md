# Panorama Workspaces And Archives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow each project to maintain multiple titled panorama workspaces, each with isolated graph data and the latest 10 restorable archives.

**Architecture:** Add a `PanoramaWorkspace` model and thread `workspace_id` through existing panorama modules, relations, code links, impact analyses, and snapshots. Existing APIs remain backward compatible by resolving missing workspace parameters to a per-project default workspace. Snapshot creation becomes workspace-scoped and prunes older archives beyond 10; restore rebuilds the current workspace from snapshot items.

**Tech Stack:** Flask, SQLAlchemy, MySQL-compatible startup migrations in `web/app/__init__.py`, existing `panorama.py` API handlers, vanilla JS template `web/templates/panorama.html`, Python unittest/pytest style tests.

---

## File Map

- Modify `web/app/models.py`
  - Add `PanoramaWorkspace`.
  - Add `workspace_id` to panorama-related models and `to_dict()`.
  - Update uniqueness comments/constraints where safe for new installs.
- Modify `web/app/__init__.py`
  - Create `panorama_workspaces`.
  - Add `workspace_id` columns if missing.
  - Backfill default workspaces and old rows.
- Modify `web/app/api/panorama.py`
  - Add workspace CRUD.
  - Add workspace resolver helpers.
  - Scope modules, relations, graph, code entities, links, impact, snapshots, and changes by workspace.
  - Add snapshot restore and retention.
- Modify `web/templates/panorama.html`
  - Add title selector and create-title modal/action.
  - Pass `workspace_id` on all relevant API calls.
  - Scope snapshot list and restore action.
- Modify `web/app/services/panorama_snapshot.py`
  - Ensure snapshot item keys support workspace-scoped restore.
- Tests
  - Add/extend `tests/test_panorama_graph.py` or new `tests/test_panorama_workspaces.py`.

## Task 1: Model And Migration

- [ ] Add `PanoramaWorkspace` model in `web/app/models.py`.
- [ ] Add `workspace_id` fields to:
  - `GameModulePanorama`
  - `GameModuleRelation`
  - `PanoramaCodeEntity`
  - `PanoramaModuleCodeLink`
  - `PanoramaImpactAnalysis`
  - `PanoramaSnapshot`
- [ ] Include `workspace_id` in each affected `to_dict()`.
- [ ] Add startup migration in `web/app/__init__.py`:
  - Create `panorama_workspaces`.
  - Add missing `workspace_id` columns.
  - Create one default workspace per project found in existing panorama rows/snapshots.
  - Backfill all old rows to the matching default workspace.
- [ ] Run syntax check:
  - `python -m py_compile web/app/models.py web/app/__init__.py`

## Task 2: Workspace Resolver And CRUD API

- [ ] In `web/app/api/panorama.py`, add `_get_or_create_default_workspace(project_id)`.
- [ ] Add `_resolve_workspace(project_id=None, workspace_id=None, title=None, create=False)`.
- [ ] Add `GET /panorama/workspaces`.
- [ ] Add `POST /panorama/workspaces`.
- [ ] Add `PUT /panorama/workspaces/<id>`.
- [ ] Add guarded `DELETE /panorama/workspaces/<id>`.
- [ ] Test manually with:
  - Create `程序功能`.
  - Create `配置模块`.
  - Ensure duplicate title in same project is rejected or returns existing.

## Task 3: Scope Existing API By Workspace

- [ ] Update module list/create/batch upsert:
  - Resolve workspace.
  - Filter by `workspace_id`.
  - Upsert by `workspace_id + path`.
- [ ] Update clear highlights to scope by workspace.
- [ ] Update relation validation:
  - Source and target must share `workspace_id`.
  - Relation upsert unique lookup includes `workspace_id`.
- [ ] Update graph API:
  - Filter modules and relations by workspace.
- [ ] Update code entity and module-code link APIs:
  - Filter/upsert by workspace.
- [ ] Update impact analysis:
  - Analyze only current workspace.
  - Store `workspace_id`.
- [ ] Update change list:
  - When filtering by project, also support `workspace_id`.
- [ ] Regression test:
  - Same project, two workspaces, same module path should create two different modules.

## Task 4: Workspace-Scoped Archives

- [ ] Update `_collect_project_panorama_state()` into workspace-aware collection.
- [ ] Update snapshot list/create/detail/diff to use `workspace_id`.
- [ ] After snapshot creation, prune older snapshots beyond 10 for that workspace.
- [ ] Add `POST /panorama/snapshots/<id>/restore`.
- [ ] Implement restore:
  - Delete current workspace modules/relations/links.
  - Recreate modules from snapshot items.
  - Rebuild parent-child and relation references using stable item keys/path mapping.
- [ ] Tests:
  - Creating 11 snapshots leaves 10.
  - Restore returns module count and relation count.

## Task 5: Frontend Workspace Selector

- [ ] Add workspace selector beside project selector in `web/templates/panorama.html`.
- [ ] Load workspaces after project changes.
- [ ] Persist selected workspace in localStorage by project.
- [ ] Add create workspace flow.
- [ ] Append `workspace_id` to:
  - modules
  - graph
  - snapshots
  - impact analysis
  - changes
  - module create/edit
  - clear highlights
- [ ] Add restore button in snapshot UI with confirmation.
- [ ] Manual UI smoke:
  - Select project.
  - Create two titles.
  - Add modules under each.
  - Verify switching isolates data.
  - Create and restore archive.

## Task 6: Verification And Experience Record

- [ ] Run focused tests:
  - `python -m unittest tests.test_panorama_graph`
  - `python -m unittest tests.test_panorama_workspaces` if created
- [ ] Run `ReadLints` for modified files.
- [ ] Deploy to Hub with a small deploy script.
- [ ] Verify remote:
  - `openclaw-web` active.
  - Existing panorama data appears under default workspace.
  - New title can be created and isolated.
- [ ] Update `docs/经验记录.md` with migration and usage notes.

