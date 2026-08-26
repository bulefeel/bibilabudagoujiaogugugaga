"""One JS bundle serves every page; a page-specific block must not break others."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src/ziniao_automation"


def _script() -> str:
    return (ROOT / "static/app.js").read_text(encoding="utf-8")


def test_top_level_dom_lookups_are_guarded_before_use() -> None:
    """A missing element on the current page must not throw at load time.

    ``$$``'s ``root = document`` default applies to ``undefined`` only, never to
    an explicit ``null``.  Passing a not-found element therefore reaches
    ``null.querySelectorAll`` and throws — and because everything runs inside
    one IIFE, every handler registered *after* that line silently never binds.

    Field case 2026-08-25: ``updateSelectedStoreCount($("[data-schedule-form]"))``
    ran on /diagnostics, where no schedule form exists.  The credential form's
    submit handler (declared further down) never bound, so saving fell back to a
    native form submit: page reloaded to the top, no request, no toast, nothing
    in the log — indistinguishable from "the button is broken".
    """

    script = _script()
    # Top-level statements: exactly two spaces of indent inside the IIFE.
    offenders: list[str] = []
    for line in script.splitlines():
        if not re.match(r"^  [A-Za-z_$]", line):
            continue
        if re.match(r"^  (function|const|let|var|class|async function|//)", line):
            continue
        # A bare $(...) result handed straight to a call is the risky shape.
        for call in re.finditer(r"(\w+)\(\s*\$\((\"|')\[[^)]+\)\s*\)", line):
            if "?." not in line:
                offenders.append(line.strip()[:120])
    assert not offenders, (
        "顶层把可能为 null 的 $() 结果直接传给函数：\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("kind", ["ziniao", "feishu"])
def test_credential_forms_cannot_fall_back_to_a_native_submit(kind: str) -> None:
    """The save control must be inert when JS fails, never a page navigation.

    A ``type="submit"`` button inside a ``<form>`` reloads the page when no
    handler intercepts it — which looks like success to nobody and loses the
    typed password.  ``type="button"`` degrades to "nothing happened", which is
    at least honest.
    """

    page = (ROOT / "templates/diagnostics.html").read_text(encoding="utf-8")
    form = re.search(
        rf'<form[^>]*data-settings-form="{kind}"[^>]*>.*?</form>', page, re.S
    )
    assert form, f"{kind} 凭据表单不在诊断页里"
    block = form.group(0)
    assert 'onsubmit="return false"' in block, "表单必须挡住原生提交"
    assert f'data-settings-save="{kind}"' in block, "保存按钮需要可绑定的钩子"
    save_button = re.search(r'<button[^>]*data-settings-save[^>]*>', block)
    assert save_button and 'type="button"' in save_button.group(0), (
        "保存按钮必须是 type=button，否则 JS 挂掉时会退化成原生提交"
    )
    assert 'type="submit"' not in block, "凭据表单里不允许出现 submit 控件"


def test_the_save_handler_is_bound_to_the_button_not_the_form() -> None:
    script = _script()
    assert 'data-settings-save="${kind}"' in script
    assert 'form.addEventListener("submit"' not in script.split("bindSettings")[1][:600]


def test_the_save_handler_does_not_look_up_a_submit_button_it_removed() -> None:
    """The template and the handler have to agree on what the button is.

    Changing the save controls to ``type="button"`` (so a script failure gives
    an inert button instead of a native form submit) left the handler still
    doing ``$('button[type="submit"]', form)``.  That returned null and
    ``submit.disabled = true`` threw on every click — reproducing the exact
    "button does nothing" symptom the type change was meant to remove, this
    time with an uncaught TypeError in the console.
    """

    script = _script()
    start = script.index("function bindSettings")
    body = script[start : script.index("bindSettings(\"ziniao\"")]
    # Match the call, not the words: the comment above the fix quotes the old
    # selector on purpose, and a substring test would fail on the explanation.
    assert not re.search(r"""\$\(\s*['"]button\[type=.submit.\]""", body), (
        "保存按钮已经不是 submit 了，处理器不能再按 type=submit 查找"
    )
    assert "event.currentTarget" in body, "应当直接用被点击的按钮"
