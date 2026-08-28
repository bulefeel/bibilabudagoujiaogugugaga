from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _script() -> str:
    return (PROJECT_ROOT / "src/ziniao_automation/static/app.js").read_text(
        encoding="utf-8"
    )


def _template() -> str:
    return (PROJECT_ROOT / "src/ziniao_automation/templates/stores.html").read_text(
        encoding="utf-8"
    )


def test_store_editor_uses_one_unified_setup_action_without_a_save_prerequisite() -> None:
    template = _template()
    script = _script()
    # Slice to whichever handler comes next rather than naming one, so adding
    # an unrelated action after this one cannot silently widen the assertions
    # below onto a handler they were never about.
    start = script.index('if (action === "detect-store-setup")')
    action = script[start : start + 1 + script[start + 1 :].index('if (action === "')]

    assert template.count('data-action="detect-store-setup"') == 1
    assert "自动获取卖家ID并核验站点" in template
    assert "程序只打开一次对应紫鸟店铺" in template
    assert "身份一致时会自动确认" in template
    assert "请先勾选至少一个需要统一建档的站点" in action
    assert "/detect-store-setup" in action
    assert "persistStoreDraft(form)" not in action
    assert "identity_confirmed" not in action
    assert "expected_seller_id" not in action




def test_detected_identity_is_auto_confirmed_only_from_server_proof() -> None:
    template = _template()
    script = _script()
    renderer = script[
        script.index("function renderStoreSetupIdentity") :
        script.index("function renderStoreSetupResults")
    ]

    assert "function renderStoreSetupIdentity(form, data)" in renderer
    assert "input.value = sellerId" in renderer
    assert 'if (status === "SUCCEEDED" && sellerId)' in renderer
    assert "confirmed.checked = identity.identity_confirmed" in renderer
    assert "enabled.checked = identity.store_enabled" in renderer
    assert "confirmed.checked = true" not in renderer
    assert "enabled.checked = true" not in renderer
    assert "卖家身份已确认" in template
    assert "统一建档成功后自动勾选" in template
    assert "身份冲突时保留原建档" in template




def test_probe_results_do_not_clear_the_operator_site_selection() -> None:
    template = _template()
    script = _script()

    # Detection reports readiness; it must not silently change which sites the
    # operator selected for future runs.
    assert "enabled.checked = false" not in script
    assert "也不会取消你勾选的站点" in template
    assert "三种模式均可保存和运行" in template


def test_bulk_setup_is_serial_and_waiting_auth_can_resume_or_skip() -> None:
    template = _template()
    script = _script()

    assert 'data-action="detect-all-store-setups"' in template
    assert "一键自动建档全部店铺" in template
    assert "每次只打开一家紫鸟店铺" in template
    assert "async function runNextBulkStoreSetup()" in script
    assert "bulkStoreSetup.index += 1" in script
    assert "await runNextBulkStoreSetup()" in script
    assert 'data-action="continue-bulk-store-setup"' in template
    assert 'data-action="skip-bulk-store-setup"' in template
    assert "自动登录未通过" in script
    assert "再次尝试自动登录并继续" in script
    assert "队列不会打开下一家店" in script
    assert "authCountedProbeId" in script
    assert "bulkStoreSetup.authCountedProbeId !== probeId" in script
    assert "已成功建档的数据已写入SQLite；下方店铺卡片将在队列结束后统一刷新。" in script
    assert "Promise.all" not in script


def test_auth_continue_controls_follow_the_latest_probe_status() -> None:
    script = _script()

    single_continue = script[
        script.index('if (action === "continue-store-setup")') :
        script.index('if (action === "cancel-store-setup")')
    ]
    bulk_continue = script[
        script.index('if (action === "continue-bulk-store-setup")') :
        script.index('if (action === "skip-bulk-store-setup")')
    ]

    assert 'form.dataset.storeSetupProbeStatus === "WAITING_AUTH"' in single_continue
    assert "hideStoreSetupAuth(form)" in single_continue
    assert 'bulkStoreSetup.probeStatus === "WAITING_AUTH"' in bulk_continue
    assert "ui.actions.hidden = !stillWaitingForAuth" in bulk_continue
    assert "target.disabled = !stillWaitingForAuth" in bulk_continue
    assert 'bulkStoreSetup.probeStatus = "CHECKING"' in script
    assert "ui.actions.hidden = true" in script
    assert 'resume: $(\'[data-action="continue-bulk-store-setup"]\'' in script


def test_bulk_setup_includes_unconfirmed_stores_and_defaults_empty_sites() -> None:
    template = _template()
    script = _script()

    assert "data-identity-state" in template
    assert "data-store-setup-needed" in template
    assert "data-store-setup-marketplaces" in template
    assert "data-store-setup-defaulted" in template
    assert 'const cards = allCards.filter(card => card.dataset.storeSetupNeeded === "true")' in script
    assert 'if (defaulted) marketplaceCodes = ["CA", "UK", "AU"]' in script
    assert "尚未选择站点，将默认按 CA、UK、AU 三站建档" in script
    assert "另有 ${alreadyComplete} 家已完成，将跳过" in script
    assert "identityState:String(card.dataset.identityState" in script


def test_bulk_queue_is_persistent_and_network_errors_keep_current_position() -> None:
    script = _script()

    assert 'const bulkSetupStorageKey = "ziniao.storeSetupQueue.v2"' in script
    assert "persistBulkStoreSetup" in script
    assert "restoreBulkStoreSetup" in script
    assert "sessionStorage.setItem" in script
    assert "attempt < 180" in script
    assert "await sleep(2000)" in script
    assert "页面已持续追踪 6 分钟" in script
    assert "批量队列暂停在 ${store.name}" in script
    assert "从当前未完成位置继续" in script
    assert "当前店铺位置已保留" in script


def test_global_credential_error_pauses_single_and_bulk_setup_without_store_failure() -> None:
    script = _script()
    base = (PROJECT_ROOT / "src/ziniao_automation/templates/base.html").read_text(
        encoding="utf-8"
    )
    single = script[
        script.index('if (action === "detect-store-setup")') :
        script.index('if (action === "continue-store-setup")')
    ]
    bulk = script[
        script.index("async function runNextBulkStoreSetup") :
        script.index("function formData")
    ]

    assert 'const ziniaoCredentialErrorCode = "ZINIAO_CREDENTIALS_INVALID"' in script
    assert "pauseStoreSetupForCredential(form, exc)" in single
    assert "未将该店计为核验失败" in script
    assert "pauseBulkStoreSetupForCredential(store, exc)" in bulk
    assert "bulkStoreSetup.index += 1" not in bulk
    assert "bulkStoreSetup.failed += 1" not in bulk
    assert "凭据保存后重试当前店铺" in script
    assert "不会继续打开下一家" in script
    assert "app.js') }}?v=20260828-feedback-removal" in base


def test_bulk_refresh_revalidates_probe_before_showing_auth_actions() -> None:
    script = _script()
    restore = script[
        script.index("function restoreBulkStoreSetup") :
        script.index("function bulkSetupUi")
    ]

    assert "ui.actions.hidden = true" in restore
    assert "ui.resume.disabled = true" in restore
    assert "ui.skip.disabled = true" in restore
    assert "ui.start.disabled = false" in restore


def test_bulk_terminal_result_and_queue_position_are_persisted_atomically() -> None:
    script = _script()
    summarize = script[
        script.index("function summarizeBulkStoreResult") :
        script.index("function persistBulkSetupCompletionSummary")
    ]
    start = script.index("async function handleBulkStoreSetupResponse")
    terminal = script[start : script.index('if (status === "WAITING_AUTH")', start)]

    assert "persistBulkStoreSetup()" not in summarize
    assert terminal.index("bulkStoreSetup.index += 1") < terminal.index(
        "persistBulkStoreSetup()"
    )


def test_expired_bulk_auth_probe_can_be_skipped_without_trapping_queue() -> None:
    script = _script()
    skip = script[
        script.index('if (action === "skip-bulk-store-setup")') :
        script.index('if (action === "reset-store-setup")')
    ]

    assert "exc.statusCode !== 404" in skip
    assert "原自动建档现场已过期" in skip
    assert 'bulkStoreSetup.probeStatus = "CANCELLED"' in skip
    assert "bulkStoreSetup.index += 1" in skip


def test_terminal_store_failures_continue_to_the_next_store() -> None:
    script = _script()
    handler = script[
        script.index("async function handleBulkStoreSetupResponse") :
        script.index("async function runNextBulkStoreSetup")
    ]

    assert "storeSetupTerminalStatuses.has(status)" in handler
    assert "summarizeBulkStoreResult(data)" in handler
    assert "bulkStoreSetup.index += 1" in handler
    assert "await runNextBulkStoreSetup()" in handler
    assert '"FAILED", "CANCELLED", "UNAVAILABLE", "NEEDS_REVIEW", "SKIPPED"' in script


def test_bulk_summary_counts_identity_separately_and_refreshes_cards() -> None:
    template = _template()
    script = _script()

    assert "身份已建档" in template
    # "其中" and "次" are load-bearing. siteIncomplete is incremented inside the
    # success branch, so it is a subset, and auth counts pauses rather than
    # stores. Rendered as four equal tiles all reading as "家", a three-store
    # queue showed 2+1+1+0 and looked like the program could not add up.
    assert "其中站点未通过" in template
    assert "<b data-bulk-store-setup-auth>0</b> 次" in template
    # The bucket counts stores whose identity never got bound; an explicit skip
    # after the identity was already committed is not one of them.
    assert "身份未建档" in template
    assert "身份失败或跳过" not in template
    assert "function identitySetupSucceeded(data)" in script
    assert "function siteSetupNeedsFollowup(data)" in script
    assert "bulkStoreSetup.siteIncomplete += 1" in script
    assert 'const bulkSetupSummaryStorageKey = "ziniao.storeSetupCompleted.v1"' in script
    assert "persistBulkSetupCompletionSummary()" in script
    assert "restoreBulkSetupCompletionSummary()" in script
    assert "setTimeout(() => location.reload(), 100)" in script
    assert "下方店铺卡片已刷新为数据库最新状态" in script


def test_a_restored_bulk_queue_is_checked_against_the_cards_on_the_page() -> None:
    """sessionStorage records a belief; the cards record the database.

    Between two loads of this page the operator can undo the very run the queue
    is describing — 「删除 / 重置建档」 clears the identity and then reloads this
    same tab — and the archive survived untouched. The banner went on announcing
    「身份已建档 2」 above three cards that all read 「尚未绑定」, with no way to
    tell which one was lying.
    """

    script = _script()

    check = script[
        script.index("function bulkQueueArchiveIsStale") :
        script.index("function discardBulkStoreSetup")
    ]
    # Reconciled against the server-rendered cards, not against itself.
    assert '$$("[data-store-card]")' in check
    assert "dataset.identityState" in check
    assert "return success > boundSoFar;" in check
    assert "queue.some(store => !cards.has(store.id))" in check

    restore = script[
        script.index("function restoreBulkStoreSetup") :
        script.index("function bulkSetupUi")
    ]
    assert "bulkQueueArchiveIsStale(queue, index, Number(saved.success || 0))" in restore
    assert restore.index("bulkQueueArchiveIsStale") < restore.index("Object.assign"), (
        "必须在把陈旧计数搬进 bulkStoreSetup 之前就判定，否则横幅已经渲染出假数字"
    )


def test_an_unfinished_bulk_queue_can_always_be_abandoned() -> None:
    """A restored queue hijacks the toolbar button, so it needs a way out.

    While one exists, 「一键自动建档全部店铺」 resumes it instead of starting a
    fresh pass, and the only branch that re-picks eligible stores and zeroes the
    counters is unreachable. Nothing cleared the archive on operator request, so
    closing the tab was the sole escape — and no text on screen said so.
    """

    template = _template()
    script = _script()

    assert 'data-action="discard-bulk-store-setup"' in template
    assert "放弃这个队列" in template
    assert "data-bulk-store-setup-dismiss hidden" in template, "默认必须隐藏"

    discard = script[
        script.index("function discardBulkStoreSetup") :
        script.index("function restoreBulkStoreSetup")
    ]
    assert "sessionStorage.removeItem(bulkSetupStorageKey)" in discard
    assert "resumeAvailable: false" in discard
    assert 'ui.start.textContent = "一键自动建档全部店铺"' in discard, (
        "放弃之后工具栏按钮必须变回全新一轮的文案"
    )
    assert 'if (action === "discard-bulk-store-setup")' in script
    # Shown only for a restored queue; a live pass must use 「跳过此店并继续」 so
    # an open Ziniao window is never stranded.
    assert "if (ui.dismiss) ui.dismiss.hidden = false;" in script
    assert script.count("if (ui.dismiss) ui.dismiss.hidden = true;") >= 2


def test_store_setup_can_be_deleted_and_reset() -> None:
    template = _template()
    script = _script()

    assert 'data-action="reset-store-setup"' in template
    assert "删除 / 重置建档" in template
    assert 'if (action === "reset-store-setup")' in script
    assert "/setup`" in script
    assert 'method:"DELETE"' in script
    assert "卖家 ID、身份确认、启用状态将清空" in script
    assert "紫鸟店铺环境、历史运行记录与资金记录都会保留" in script


def test_legacy_setup_routes_are_not_left_in_the_frontend_and_cache_is_bumped() -> None:
    template = _template()
    script = _script()
    base = (PROJECT_ROOT / "src/ziniao_automation/templates/base.html").read_text(
        encoding="utf-8"
    )

    combined = template + script
    for legacy in (
        "detect-identity",
        "identity-probes",
        "detect-marketplace-setup",
        "marketplace-setup-probes",
        "data-marketplace-setup",
        "detect-all-marketplace-setups",
    ):
        assert legacy not in combined
    assert "20260828-feedback-removal" in base
