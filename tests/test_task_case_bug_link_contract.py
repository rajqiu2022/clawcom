from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "web" / "app" / "models.py"
INIT = ROOT / "web" / "app" / "__init__.py"
API = ROOT / "web" / "app" / "api" / "testplans.py"
TAPD = ROOT / "web" / "app" / "api" / "tapd.py"
TPL = ROOT / "web" / "templates" / "testplans.html"


def test_task_case_model_exposes_bug_sync_fields_and_migration():
    models = MODELS.read_text(encoding="utf-8")
    init = INIT.read_text(encoding="utf-8")

    for field in (
        "tapd_bug_url",
        "case_info_snapshot",
        "bug_sync_status",
        "bug_sync_error",
        "bug_synced_at",
    ):
        assert field in models
        assert field in init

    assert "'case_info_snapshot': self.case_info_snapshot" in models
    assert "'bug_sync_status': self.bug_sync_status" in models


def test_update_task_case_supports_partial_note_bug_url_and_retry_route():
    source = API.read_text(encoding="utf-8")
    update_route = source.split("def update_task_case(plan_id, task_id, tc_id):", 1)[1].split(
        "@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases/batch'",
        1,
    )[0]

    assert "status 为必填项" not in update_route
    assert "tapd_bug_url" in update_route
    assert "sync_task_case_bug_link(" in source
    assert "bug_sync_status = 'failed'" in source
    assert "task.tapd_bug_ids" in source
    assert "except ValueError as exc:" in update_route
    assert "return jsonify({'error': str(exc)}), 400" in update_route
    assert "retry_task_case_bug_sync" in source


def test_tapd_request_supports_post_data_and_task_bug_query_uses_base_url():
    tapd = TAPD.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")

    assert "def _tapd_request(method, url, params=None, data=None" in tapd
    assert "data=data" in tapd
    task_bugs = api.split("def get_task_tapd_bugs(plan_id, task_id):", 1)[1].split(
        "# ==================== 用例库目录树", 1
    )[0]
    assert "TAPD_API_BASE_URL" in task_bugs
    assert "https://api.tapd.cn/bugs" not in task_bugs


def test_frontend_has_case_note_bug_modal_and_sync_retry_controls():
    source = TPL.read_text(encoding="utf-8")

    assert "openCaseNoteBugModal" in source
    assert "tapd_bug_url" in source
    assert "bug_sync_status" in source
    assert "retryCaseBugSync" in source
    assert "当前用例为失败/阻塞时可关联 TAPD Bug" in source
    assert "本地已保存，TAPD 同步失败" in source
    assert "关联 Bug 链接或 ID（可选）" in source
    assert "支持完整链接、标题 + 链接、标题 + ID 或纯 ID" in source


def test_case_row_grid_has_a_column_for_bug_info_before_note_button():
    source = TPL.read_text(encoding="utf-8")
    css = source.split(".tp-case-row {", 1)[1].split("}", 1)[0]
    row = source.split("function renderCaseRow(c, taskId)", 1)[1].split(
        "function findTaskCase", 1
    )[0]

    assert "grid-template-columns: 22px 40px 1fr 52px 90px 80px minmax(70px,0.8fr) minmax(90px,0.8fr) 36px;" in css
    assert "${bugHtml}</span>" in row
    assert "addCaseNote(" in row

