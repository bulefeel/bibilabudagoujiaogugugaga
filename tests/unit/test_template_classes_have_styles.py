"""Every class a template invents must exist in the stylesheet.

Shipped twice already: the diagnostics page went out with settings-card and
diagnostic-actions markup and no rules for either, so the forms rendered as bare
unstyled controls, and the schedule form's interval row did the same — the
number and its unit both took ``width:100%`` from the dialog rule and stacked
into what looked like two unrelated fields.

Neither broke a request, so nothing in the suite noticed. String assertions on
markup do not either: they check the class is *written*, never that it *means*
anything.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = PROJECT_ROOT / "src/ziniao_automation/templates"
STYLESHEET = PROJECT_ROOT / "src/ziniao_automation/static/app.css"

# Bootstrap-free project: every class here is one this codebase defines. These
# few are structural rather than styled, and are matched by an element or
# attribute selector instead.
STRUCTURAL_ONLY = frozenset({"active", "on", "ok", "bad", "warn", "live", "word"})

# Pre-existing, each checked once and left alone on purpose. This is a ledger,
# not a mute button: anything new has to be argued into it.
KNOWN_UNSTYLED = {
    # Modifier hooks sitting on a base class that is styled. They exist so a
    # future rule has somewhere to attach; today they change nothing.
    "auth-brand",        # with .brand
    "setup-page",        # with .auth-page
    "store-target-sites",  # a <small> inside the styled .store-target-copy
    # The element carrying it is `hidden aria-hidden="true"` — copy kept for
    # reuse, never painted.
    "legacy-schedule-copy",
    # ⚠️ A real gap, not mine to invent: the 「待检测」 marketplace badge asks for
    # a state colour the stylesheet never defines, so it renders the same as
    # every other state. Cosmetic only.
    "pending",
}


def _template_classes() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        classes: set[str] = set()
        for attribute in re.findall(r'class="([^"{}]*)"', path.read_text(encoding="utf-8")):
            classes.update(part for part in attribute.split() if part)
        if classes:
            found[path.name] = classes
    return found


def _styled_classes() -> set[str]:
    return set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", STYLESHEET.read_text(encoding="utf-8")))


def test_every_class_used_in_a_template_is_styled() -> None:
    styled = _styled_classes()
    missing: list[str] = []
    for template, classes in _template_classes().items():
        for name in sorted(classes - styled - STRUCTURAL_ONLY - KNOWN_UNSTYLED):
            missing.append(f"{template}: .{name}")
    assert not missing, (
        "模板用了 app.css 里没有的类，页面上会是没有样式的裸控件：\n"
        + "\n".join(missing)
    )


@pytest.mark.parametrize(
    "selector",
    [
        # The interval row is the reason this file exists: without a grid the
        # period and its unit each take a full row and stop reading as one field.
        ".interval-row",
        ".fieldset-help.warn",
    ],
)
def test_the_schedule_interval_controls_are_actually_laid_out(selector: str) -> None:
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    assert selector in stylesheet


def test_the_interval_row_puts_the_unit_beside_the_number() -> None:
    """A one-column grid would reproduce the bug this is guarding against."""

    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    rule = stylesheet[stylesheet.index(".interval-row{") :]
    rule = rule[: rule.index("}")]

    assert "display:grid" in rule
    columns = re.search(r"grid-template-columns:([^;}]+)", rule)
    assert columns is not None
    assert len(columns.group(1).split()) >= 2, "单位必须和数字并排，不能各占一行"
