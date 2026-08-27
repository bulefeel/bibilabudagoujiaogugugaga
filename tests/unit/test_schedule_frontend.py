from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_schedule_template_exposes_crud_and_run_now_actions() -> None:
    template = (PROJECT_ROOT / "src/ziniao_automation/templates/schedules.html").read_text(
        encoding="utf-8"
    )

    assert 'data-action="edit-schedule"' in template
    assert 'data-action="run-schedule-now"' in template
    assert 'data-action="delete-schedule"' in template
    assert "立即执行" in template
    assert "删除" in template
    # The form is an anchor plus a period now, not a wall clock plus weekdays:
    # Amazon counts its 24-hour payout cap from the previous request, so a daily
    # wall-clock rule always fell a few seconds short and was refused.
    assert "首次运行时间（UTC+8）" in template
    assert 'name="first_run_at"' in template
    assert "data-schedule-interval-value" in template
    assert "data-schedule-interval-unit" in template
    assert "滑动 24 小时" in template, "表单要说明限流是从上一次请求起算的"
    assert "执行时间（UTC+8）" not in template
    assert 'name="run_days"' not in template
    assert "至少选择一个" in template
    assert "只读检查（dry_run）" in template
    assert "人工审核（approval）" in template
    assert "全自动（auto）" in template
    assert "账户尾号待建档" in template
    assert "无付款数据或可提现为 0 时会正常跳过" in template
    assert "已启用" in template
    assert "运行中' if item.enabled" not in template
    assert "按计划时间持久排队" in template
    assert "即使轮到时已经跨日，也会继续执行" in template


def test_run_pages_distinguish_durable_queue_from_execution() -> None:
    list_template = (
        PROJECT_ROOT / "src/ziniao_automation/templates/runs.html"
    ).read_text(encoding="utf-8")
    detail_template = (
        PROJECT_ROOT / "src/ziniao_automation/templates/run_detail.html"
    ).read_text(encoding="utf-8")

    assert "run.queue_position" in list_template
    assert "原计划时间" in list_template
    assert "排队或执行" in list_template
    assert "run.queue_state|queue_state_label" in list_template
    assert "实际开始" in detail_template
    assert "任务正在持久排队" in detail_template
    assert "任务已获得执行权" in detail_template
    assert "run.queue_action|queue_action_label" in detail_template


def test_schedule_frontend_uses_expected_api_contracts_and_validates_choices() -> None:
    script = (PROJECT_ROOT / "src/ziniao_automation/static/app.js").read_text(
        encoding="utf-8"
    )

    assert "`/api/schedules/${id}/run-now`" in script
    assert 'method:"DELETE"' in script
    assert 'method:editingId ? "PATCH" : "POST"' in script
    assert "请至少勾选一个站点" in script
    assert "请选择首次运行时间。" in script
    assert "运行间隔必须至少 1 分钟。" in script
    # datetime-local yields a bare wall clock; the label promises UTC+8, so the
    # offset is pinned rather than left for the server to guess.
    assert 'return raw?raw+":00+08:00":""' in script
    assert "days_of_week" not in script
    assert "run_days" not in script
    assert "当前模式：${modeLabel}" in script
    assert "已有运行记录不会被删除" in script


def test_schedule_marketplaces_follow_the_selected_store_configuration() -> None:
    template = (PROJECT_ROOT / "src/ziniao_automation/templates/schedules.html").read_text(
        encoding="utf-8"
    )
    script = (PROJECT_ROOT / "src/ziniao_automation/static/app.js").read_text(
        encoding="utf-8"
    )

    assert "data-enabled-marketplaces" in template
    assert "selectattr(\"enabled\")" in template
    assert "data-schedule-store" in template
    assert "data-marketplace-warning" in template
    assert "未在该店启用，请编辑修正" in template
    assert 'disabled title="排期含未启用站点' in template
    assert "refreshScheduleMarketplaces" in script
    assert "input.disabled = !available" in script
    assert "旧排期包含当前未启用的站点" in script


