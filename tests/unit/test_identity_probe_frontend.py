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


def test_unified_store_setup_is_the_only_visible_identity_probe_contract() -> None:
    script = _script()
    template = _template()

    assert template.count('data-action="detect-store-setup"') == 1
    assert 'data-action="detect-identity"' not in template
    assert "detect-identity" not in script
    assert "identity-probes" not in script
    assert "storeSetupProbePath(storeId, probeId)" in script
    assert "/store-setup-probes/" in script
    assert "${storeSetupProbePath(storeId, probeId)}/continue" in script
    assert "${storeSetupProbePath(storeId, probeId)}/cancel" in script
    assert "/detect-store-setup" in script
    assert "window.open" not in script
    assert "sellercentral.amazon" not in script.lower()


def test_unified_setup_auth_controls_appear_only_for_waiting_auth() -> None:
    script = _script()
    template = _template()

    assert "data-store-setup-auth-panel hidden" in template
    assert 'data-action="continue-store-setup"' in template
    assert 'data-action="cancel-store-setup"' in template
    assert "邮箱 Continue" in template
    assert "紫鸟托管 Passkey" in template
    assert "已填密码登录" in template
    assert "6 位 OTP" in template
    assert "每个页面动作最多自动点击 3 次" in template
    assert "每个站点最多进行 3 轮完整验证" in template
    assert "CA、UK、AU 分别计数" in template
    assert "只有次数用尽" in template
    assert "未填内容、候选不唯一、CAPTCHA、非托管 Passkey" in template
    assert "不会换到普通 Chrome" in template
    assert 'if (status === "WAITING_AUTH") { showStoreSetupAuth(form, data); return; }' in script
    assert "showStoreSetupAuth(form, data)" in script
    assert "ui.panel.hidden = false" in script
    assert "ui.panel.hidden = true" in script
    assert "再次尝试自动登录并继续" in template


def test_detected_identity_uses_authoritative_auto_confirmation_flags() -> None:
    script = _script()
    template = _template()
    renderer = script[
        script.index("function renderStoreSetupIdentity") :
        script.index("function renderStoreSetupResults")
    ]

    assert "function renderStoreSetupIdentity(form, data)" in renderer
    assert 'if (status === "SUCCEEDED" && sellerId)' in renderer
    assert "input.value = sellerId" in renderer
    assert 'typeof identity.identity_confirmed === "boolean"' in renderer
    assert "confirmed.checked = identity.identity_confirmed" in renderer
    assert 'typeof identity.store_enabled === "boolean"' in renderer
    assert "enabled.checked = identity.store_enabled" in renderer
    # Confirmation comes only from the server-side same-session proof.  The
    # browser must never hard-code a successful state from seller text alone.
    assert "confirmed.checked = true" not in renderer
    assert "enabled.checked = true" not in renderer
    assert "身份一致时自动绑定并确认" in template
    assert "统一建档成功后自动勾选" in template


