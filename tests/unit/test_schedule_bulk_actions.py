"""排期页的批量启用/停用/删除。

批量操作复用单条的 PATCH / DELETE 接口逐条发，而不是新造批量接口：那两个接口里
的校验是踩过坑才对的（停用不校验站点、启用才校验，见
project_schedule-pause-must-not-validate-targets），投影刷新也在里面。

这里钉住的是三件容易悄悄坏掉的事：
1. 勾选框必须带 ``data-action`` —— 委托处理器第一行是
   ``event.target.closest("[data-action],[data-run-action]")``，没有这个属性
   就直接 return，勾选框能勾上但工具栏永远不更新
2. 新加的 class 必须有 CSS —— 这个仓库犯过三次
3. 网格列数要跟着首列勾选框加一列，否则整行错位
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "src/ziniao_automation/templates/schedules.html").read_text(
    encoding="utf-8"
)
APP_JS = (ROOT / "src/ziniao_automation/static/app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "src/ziniao_automation/static/app.css").read_text(encoding="utf-8")

BULK_ACTIONS = (
    "toggle-all-schedules",
    "bulk-enable-schedules",
    "bulk-disable-schedules",
    "bulk-delete-schedules",
    "select-schedule",
)


@pytest.mark.parametrize("action", BULK_ACTIONS)
def test_every_bulk_control_has_a_handler(action: str) -> None:
    """模板上的每个动作都要有真正的处理分支。

    断言 ``action === "x"`` 而不是裸字符串 ``"x"``：后者在按钮选择器数组里也出现，
    于是把整个 if 分支删掉测试照样绿——这条最初就是那么写的。
    """

    assert f'data-action="{action}"' in TEMPLATE, f"模板缺少 {action}"
    assert f'action === "{action}"' in APP_JS, f"app.js 没有 {action} 的处理分支"


def test_the_row_checkbox_carries_data_action() -> None:
    """没有 data-action，委托处理器在第一行就 return 了。

    ``event.target.closest("[data-action],[data-run-action]")`` 找不到祖先就返回
    null。勾选框仍会被浏览器勾上，但计数和按钮禁用状态永远不刷新——看起来像
    "勾了没反应"。
    """

    block = TEMPLATE[TEMPLATE.index("data-schedule-select") - 200 :][:400]
    assert 'data-action="select-schedule"' in block


def test_the_row_checkbox_carries_what_the_receipt_needs() -> None:
    """批量操作要跳过本来就处于目标状态的条目，并按名字报告失败。"""

    block = TEMPLATE[TEMPLATE.index("data-schedule-select") :][:400]
    for attribute in ("data-schedule-name", "data-schedule-enabled", 'value="{{ item.id }}"'):
        assert attribute in block, f"勾选框缺少 {attribute}"


def test_bulk_buttons_start_disabled() -> None:
    """一条都没勾时点下去只会得到"没反应"。"""

    for action in ("bulk-enable-schedules", "bulk-disable-schedules", "bulk-delete-schedules"):
        line = next(
            row for row in TEMPLATE.splitlines() if f'data-action="{action}"' in row
        )
        assert "disabled" in line, f"{action} 应当默认禁用"


def test_every_schedule_row_grid_reserves_the_checkbox_column() -> None:
    """**每一条** .schedule-row 栅格规则都要留出勾选列，媒体查询里的也算。

    这条最初只检查了基础规则，于是漏掉了 ≤1250px 和 ≤760px 两条覆盖——那两条
    还是加勾选框之前的列数，勾选框占掉首轨，整行内容全部左移一格。自我检验时
    才被发现，正是这条测试本该抓住的东西。
    """

    rules = re.findall(r"\.schedule-row\{grid-template-columns:([^;}]+)", APP_CSS)
    assert len(rules) >= 4, f"只找到 {len(rules)} 条栅格规则，选择器可能变了"
    for spec in rules:
        columns = spec.split()
        assert columns[0] == "26px", (
            f"首轨应为 26px 的勾选列，实际是 {columns[0]}（整条规则：{spec.strip()}）"
        )


def test_no_later_rule_collapses_the_row_to_the_checkbox_column() -> None:
    """同断点重复定义会让后写的赢；曾经因此把正文塞进 26px 的勾选列。"""

    rules = re.findall(r"\.schedule-row\{grid-template-columns:([^;}]+)", APP_CSS)
    for spec in rules:
        assert len(spec.split()) >= 3, f"勾选列之外至少还要两轨，实际：{spec.strip()}"


@pytest.mark.parametrize("name", ["row-check", "bulk-check", "bulk-count"])
def test_new_classes_have_styles(name: str) -> None:
    """模板加了 class 就得加 CSS——这个仓库犯过三次。"""

    assert re.search(rf"\.{re.escape(name)}[{{,:\s]", APP_CSS), f"缺少 .{name} 的样式"


def test_bulk_enable_sends_an_absolute_state_not_a_toggle() -> None:
    """批量启用/停用发的是目标状态，不是逐条取反。

    混选时逐条取反会把已启用的关掉、已停用的打开——正好和按钮上写的相反。
    """

    body = APP_JS[APP_JS.index('action === "bulk-enable-schedules"') :][:2000]
    assert "JSON.stringify({enabled: enable})" in body
    assert "!item.enabled" not in body, "不应逐条取反"


def test_a_failed_item_does_not_abort_the_rest() -> None:
    """批量启用尤其如此：某个站点被关掉的排期会被 422 拒，同批其余完全正常。"""

    body = APP_JS[APP_JS.index("async function runScheduleBulk") :][:1400]
    assert "for (const item of items)" in body
    assert "catch" in body and "failures.push" in body
    # 回执必须同时给出成功数和失败数，不能只说"完成了"。
    assert "failures.length" in body and "done" in body


def branch_body(anchor: str) -> str:
    """从某个 ``if (action === ...)`` 取到**下一个**分支为止。

    固定长度的窗口会越过分支边界，把隔壁单条处理器的 ``confirmAction`` 算进来，
    于是即便本分支根本没有确认框，测试也是绿的。
    """

    start = APP_JS.index(anchor)
    nxt = APP_JS.find("\n    if (action === ", start + len(anchor))
    return APP_JS[start : nxt if nxt > 0 else start + 3000]


@pytest.mark.parametrize(
    "anchor, label",
    [
        ('if (action === "bulk-delete-schedules")', "批量删除"),
        # 停用/启用共用一个分支；确认只在停用那一侧，启用不该拦。
        ('if (action === "bulk-enable-schedules" || action === "bulk-disable-schedules")', "批量停用"),
    ],
)
def test_destructive_bulk_actions_confirm_first(anchor: str, label: str) -> None:
    """删除和停用都会打断正在进行的事，必须二次确认。"""

    assert "confirmAction" in branch_body(anchor), f"{label} 缺少二次确认"


def test_enabling_in_bulk_is_not_gated_behind_a_confirm() -> None:
    """启用是扩张性操作，不该拦——单条的开关也只在暂停时确认。"""

    body = branch_body(
        'if (action === "bulk-enable-schedules" || action === "bulk-disable-schedules")'
    )
    confirm_line = next(line for line in body.splitlines() if "confirmAction" in line)
    assert "!enable" in confirm_line, "确认必须只在停用那一侧"


def test_the_confirm_dialog_resets_its_return_value() -> None:
    """按 Esc 关闭时 HTML 规范**不**写 returnValue，它会保留上一次的值。

    上一次点过「确认」而那次请求失败没跳转的话，之后每次按 Esc 都被读成确认——
    批量删除按 Esc 反而全删了。自我检验把它评为阻断级，根因在 confirmAction，
    是这次的批量操作把影响面从一行放大到 N 行。
    """

    body = APP_JS[APP_JS.index("async function confirmAction") :][:900]
    reset = body.index('dialog.returnValue = ""')
    show = body.index("dialog.showModal()")
    assert reset < show, "必须在 showModal() 之前清空 returnValue"


def test_a_bulk_run_latches_the_controls() -> None:
    """执行期间点勾选框不能把刚禁用的按钮又放开。

    refreshScheduleBulkBar() 按勾选数重算 disabled，而勾选框在执行期间仍可点；
    没有共享的忙碌状态，就能在上一批的 reload 定时器触发前再发一批。
    """

    assert "let scheduleBulkBusy" in APP_JS
    body = APP_JS[APP_JS.index("function refreshScheduleBulkBar") :][:1200]
    assert "scheduleBulkBusy || picked.length === 0" in body
    assert "box.disabled = scheduleBulkBusy" in body


def test_a_bulk_receipt_reports_every_failure_not_just_the_first() -> None:
    """不同排期失败的原因往往不同，只报第一条会让人以为其余是同一个毛病。"""

    body = APP_JS[APP_JS.index("async function runScheduleBulk") :][:2200]
    assert 'failures.join("；")' in body
    assert "failures[0]" not in body


def test_a_bulk_receipt_surfaces_the_stale_scheduler_warning() -> None:
    """单条删除把 scheduler_refreshed:false 当成要警告的结果，批量不能吞掉。"""

    body = APP_JS[APP_JS.index("async function runScheduleBulk") :][:2200]
    assert "scheduler_refreshed === false" in body
    assert "定时器尚未重载" in body


def test_bulk_labels_include_the_store_name() -> None:
    """线上五条排期名字完全相同，只报名字认不出是哪几条。"""

    body = APP_JS[APP_JS.index("function selectedSchedules") :][:1200]
    assert "store ? `${store} · ${name}`" in body
    for anchor in ('if (action === "bulk-delete-schedules")',):
        assert "item.label" in branch_body(anchor)
