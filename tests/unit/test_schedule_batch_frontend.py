from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "src/ziniao_automation/templates/schedules.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "src/ziniao_automation/static/app.js").read_text(encoding="utf-8")


def test_schedule_dialog_has_two_steps_and_batch_target_picker() -> None:
    assert 'data-schedule-step="1"' in TEMPLATE
    assert 'data-schedule-step="2"' in TEMPLATE
    assert 'data-schedule-workflow' in TEMPLATE
    assert 'name="target_store_ids"' in TEMPLATE
    assert 'data-action="select-all-schedule-stores"' in TEMPLATE
    assert 'data-action="schedule-next-step"' in TEMPLATE
    assert "data-schedule-edit-submit" in TEMPLATE


def test_schedule_script_uses_registry_and_strict_batch_contract() -> None:
    assert 'api("/api/workflows")' in SCRIPT
    assert 'api("/api/schedules/batch/preview"' in SCRIPT
    assert 'api("/api/schedules/batch"' in SCRIPT
    assert "request_id:requestId" in SCRIPT
    assert "store_ids:storeIds" in SCRIPT
    assert "workflow_config:collectWorkflowConfig(form)" in SCRIPT
    assert "targets:storeIds" not in SCRIPT


def test_schedule_edit_remains_single_schedule_patch() -> None:
    assert 'method:"PATCH"' in SCRIPT
    assert "workflow_config:config" in SCRIPT
    # Workflow identity is immutable when editing an existing schedule.
    assert 'const data = {name:String' in SCRIPT
    assert "editSubmit.hidden=false" in SCRIPT
    assert '$("[data-schedule-edit-submit]", form)' in SCRIPT


def test_single_schedule_patch_and_delete_surface_projection_lag() -> None:
    delete_block = SCRIPT[
        SCRIPT.index('if (action === "delete-schedule")') :
        SCRIPT.index("const runAction")
    ]
    edit_block = SCRIPT[
        SCRIPT.index('$("[data-schedule-form]")?.addEventListener("submit"') :
        SCRIPT.index("$('[data-schedule-mode]')?.addEventListener(\"change\"")
    ]

    assert "data.scheduler_refreshed === false" in delete_block
    assert "data.warning" in delete_block
    assert "请不要重复删除" in delete_block
    assert "已删除 · 定时器待重载" in delete_block

    assert "result.scheduler_refreshed === false" in edit_block
    assert "result.warning" in edit_block
    assert "请不要重复提交" in edit_block
    assert "已保存 · 定时器待重载" in edit_block


def test_scheduler_refresh_retry_is_frozen_and_reuses_original_payload() -> None:
    assert "setScheduleRefreshPending(form,true)" in SCRIPT
    assert "form.dataset.batchPreviewPayload" in SCRIPT
    assert "await createBatchSchedule(form, savedPayload)" in SCRIPT
    assert "pending&&!control.matches('[data-schedule-submit]')" in SCRIPT


def test_dynamic_config_fails_closed_for_unsupported_structures() -> None:
    assert '["string","number","integer","boolean"].includes(type)' in SCRIPT
    assert 'holder.dataset.configUnsupported="true"' in SCRIPT
    assert 'data-config-unsupported=true' in SCRIPT
    assert "usable.length!==1" in SCRIPT
    assert 'enum:[schema.const]' in SCRIPT
    assert "__uiNullable" in SCRIPT
    assert 'empty.dataset.configValue="null"' in SCRIPT


def test_schedule_copy_and_run_now_guard_are_workflow_generic() -> None:
    assert "只读检查（不会执行实际操作）" in SCRIPT
    assert "not item.store.enabled" in TEMPLATE
