"""挑选可提交条目的纯函数。这是整条 GPSR 流程里风险最高、也是唯一测得到的一段。

为什么只有这里测得到：Katal 的 shadow DOM 必须走页面级 ``page.evaluate`` 才读得
到，而测试夹具 ``tests/workflows/fixture_dom.py`` 的 ``FixturePage`` 只有
``locator()``——没有页面级 ``evaluate``。这也正是 amazon_disbursement（走
locator 解析）有两套 HTML 夹具、而 amazon_feedback（走 evaluate）一套都没有的
原因。所以 JS 那层只抓不判，**全部判断留在这个纯函数里**。

下面的夹具不是编的：是 2026-09-12/14 在秦登友德国站实地读到的那张页面
（51 个行节点）按形状还原出来的。
"""

from __future__ import annotations

import pytest

from ziniao_automation.workflows.amazon_gpsr.config import (
    ACTION_ALREADY_REMOVED,
    EXPECTED_CELL_COUNT,
    GpsrContractError,
    KNOWN_REASONS,
    TARGET_REASON,
    select_actionable,
)


AT_RISK = "商品有移除风险\n（在 2026年9月11日 前解决问题）"
NEEDS_SUBMIT = "查看商品信息\n状态： 需要您提交信息。"


def row(
    reason: str,
    button_id: str | None,
    *,
    action: str = AT_RISK,
    title: str = "Highland-Kuh-Keksformen, 1 Stück Kunststoff-Ausstecher für Partys",
    asin_line: str = "B0HJG2CJ5F",
    sku_line: str = "JN-GDNQGMX-1GZ",
    header: bool = False,
    nested: bool = False,
    cells: list[str] | None = None,
) -> dict:
    if cells is None:
        cells = [
            reason,
            "2026年9月11日",
            f"{title}\nASIN：{asin_line}\nSKU: {sku_line}",
            "过去 12 个月无销量",
            action,
            "无影响",
            NEEDS_SUBMIT,
        ]
    return {
        "cells": cells,
        "buttonId": (
            None if button_id is None else f"ahd-action-button-{button_id}"
        ),
        "inAccordionHeader": header,
        "inAccordionBody": nested,
    }


def live_page() -> list[dict]:
    """实测那张页面的形状还原。

    三个「警告和安全信息」ASIN，每个 3 份响应式副本（共 9 个节点）；其中
    B0GWCRRDZS 的采取的操作是「商品已移除」。外加两个折叠组、EPR、增值税。
    """

    rows: list[dict] = []
    for _ in range(3):
        rows.append(row("欧洲增值税登记要求", None, cells=[
            "欧洲增值税登记要求", "2026年9月11日", "-", "不适用",
            "已发送警告", "无影响", "",
        ]))
    for _ in range(3):
        rows.append(row(TARGET_REASON, "B0HJG2CJ5F"))
        rows.append(
            row(
                TARGET_REASON,
                "B0GXZ1ZZLT",
                title="100 Kabelbinder-Halterungssockel mit 100 Schrauben",
                asin_line="B0GXZ1ZZLT",
                sku_line="QJ-Cable-Tie-Mount-Bases-Black-100set",
            )
        )
        rows.append(
            row(
                TARGET_REASON,
                "B0GWCRRDZS",
                action=ACTION_ALREADY_REMOVED,
                title="Hundehalstücher mit aufgedrucktem Design",
                asin_line="B0GWCRRDZS",
                sku_line="JN-Dog-Bandana-2pcs",
            )
        )
    # 折叠组：组标题（三个分组共用同一个数字 case id）+ 组内行（id 是 SKU）。
    for reason, nested_ids in (
        ("GPSR： 负责人详细联系信息", ["JN-GDNQGMX-1GZ", "JN-DIYZSP-QJZS-6P"]),
        ("GPSR：制造商详细联系信息", ["B0HJFVKQHM", "B0HJD4THJF"]),
    ):
        rows.append(row(
            reason + "\n\n4 受影响的 ASIN\n4 受影响的 SKU", "21862491", header=True
        ))
        for identifier in nested_ids:
            rows.append(row(reason, identifier, nested=True))
    rows.append(row("EPR: 德国 WEEE-小型设备\n\n2 受影响的 SKU", "21862491", header=True))
    return rows


# --------------------------------------------------------------------------
def test_the_live_page_yields_exactly_the_two_submittable_asins() -> None:
    """端到端的形状断言：三个目标 ASIN 里，已下架的那个不提交。"""

    result = select_actionable(live_page())

    assert [item.asin for item in result.candidates] == ["B0HJG2CJ5F", "B0GXZ1ZZLT"]
    assert result.already_removed == 1
    assert result.drift_rows == 0
    # 每个候选都被 3 份响应式副本覆盖过，去重真的发生了。
    assert [item.dom_copies for item in result.candidates] == [3, 3]
    assert result.rows_read == len(live_page())


def test_every_non_target_reason_is_left_alone() -> None:
    """运营明确要求只点「警告和安全信息」，其余一律不碰。"""

    others = KNOWN_REASONS - {TARGET_REASON}
    assert others, "白名单里必须有非目标原因，否则这条断言是空转"
    for reason in others:
        result = select_actionable([row(reason, "B0HJG2CJ5F")])
        assert result.candidates == (), reason


@pytest.mark.parametrize(
    "button_id",
    ["21862491", "JN-GDNQGMX-1GZ", "JN-Bow-Garland-Pink-1pc", "B0HJG2CJ5", "b0hjg2cj5f"],
    ids=["group-case-id", "sku", "sku-long", "nine-chars", "lowercase"],
)
def test_only_a_well_formed_asin_can_become_a_candidate(button_id: str) -> None:
    """按钮 id 的编码方案按类别不同，形状校验是唯一可靠的闸。

    组标题是 8 位数字 case id 且**三个分组共用**，负责人那类组内行是 SKU。
    两者都不能变成一次不可逆提交的目标。
    """

    result = select_actionable([row(TARGET_REASON, button_id)])
    assert result.candidates == ()
    assert result.drift_rows == 1


def test_an_accordion_header_is_never_a_candidate() -> None:
    """就算它的 id 恰好长得像 ASIN，组标题也不许点——它代表整组，有歧义。

    它也不算「目标条目」：组标题不是一个条目，算进去会让兜底闸误报整站失败。
    """

    result = select_actionable([row(TARGET_REASON, "B0HJG2CJ5F", header=True)])
    assert result.candidates == ()
    assert result.target_asins == 0


def test_a_nested_row_with_the_target_reason_is_still_eligible() -> None:
    """排除的是组**标题**，不是组内行。

    目标原因目前总是平铺行，但亚马逊哪天把它并进折叠组，逐条那一层仍然唯一、
    仍然该处理——用户拍板的就是「逐条点组内行」。
    """

    result = select_actionable([row(TARGET_REASON, "B0HJG2CJ5F", nested=True)])
    assert [item.asin for item in result.candidates] == ["B0HJG2CJ5F"]


# --------------------------------------------------------------------------
# 「商品已移除」的两道保险
# --------------------------------------------------------------------------
def test_already_removed_is_excluded_from_the_usual_column() -> None:
    result = select_actionable(
        [row(TARGET_REASON, "B0HJG2CJ5F", action=ACTION_ALREADY_REMOVED)]
    )
    assert result.candidates == ()
    assert result.already_removed == 1


def test_already_removed_is_excluded_even_after_a_column_shift() -> None:
    """只看固定下标的话，亚马逊插一列就会让这道排除静默失效。

    那之后没有任何其它判据拦得住：行还是目标原因、卡片还是「未开始」、复选框
    照样能勾、保存照样可点——于是我们会对一个已下架的商品做不可逆的合规声明。
    所以判据是「任意一格出现」，不是「第 4 格等于」。
    """

    shifted = row(TARGET_REASON, "B0HJG2CJ5F")
    # 把标记挪到另一格，模拟插列后错位；总列数仍然是 7。
    shifted["cells"][4] = "无影响"
    shifted["cells"][5] = ACTION_ALREADY_REMOVED
    assert len(shifted["cells"]) == EXPECTED_CELL_COUNT

    result = select_actionable([shifted])
    assert result.candidates == ()


def test_at_risk_wording_is_not_mistaken_for_already_removed() -> None:
    """「商品有移除风险」和「商品已移除」只差两个字，不能误伤——那是主力场景。"""

    assert ACTION_ALREADY_REMOVED not in AT_RISK
    result = select_actionable([row(TARGET_REASON, "B0HJG2CJ5F", action=AT_RISK)])
    assert [item.asin for item in result.candidates] == ["B0HJG2CJ5F"]


# --------------------------------------------------------------------------
# 结构漂移
# --------------------------------------------------------------------------
@pytest.mark.parametrize("count", [6, 8], ids=["one-fewer", "one-more"])
def test_a_row_with_the_wrong_column_count_is_drift_not_a_candidate(count: int) -> None:
    """列数变了就说明这不是我们认识的那张表，位置判据全部作废。

    而且当**每一个**目标条目都被闸挡下时必须响亮失败——那是「看到了活儿却一件
    也干不了」，报成「没有待办」等于让这家店永远显示成功。
    """

    cells = [TARGET_REASON] + ["x"] * (count - 1)
    with pytest.raises(GpsrContractError):
        select_actionable([row(TARGET_REASON, "B0HJG2CJ5F", cells=cells)])


def test_one_drifted_row_does_not_stop_the_rows_that_still_parse() -> None:
    """只有一部分漂移时照常干活，并把漂移条数报出来供人核对。"""

    good = row(TARGET_REASON, "B0HJG2CJ5F")
    bad = row(
        TARGET_REASON, "B0GXZ1ZZLT", cells=[TARGET_REASON] + ["x"] * 5
    )
    result = select_actionable([good, bad])
    assert [item.asin for item in result.candidates] == ["B0HJG2CJ5F"]
    assert result.drift_rows == 1


def test_an_unrecognised_action_wording_is_refused_rather_than_guessed() -> None:
    """「采取的操作」用白名单而不是黑名单。

    黑名单是 fail-open 的：亚马逊把「商品已移除」改成别的说法，被排除的行立刻
    变成候选，于是对已下架商品发出不可逆声明。白名单反过来——认不出就不做。
    """

    with pytest.raises(GpsrContractError):
        select_actionable(
            [row(TARGET_REASON, "B0HJG2CJ5F", action="商品面临下架风险")]
        )


def test_unrecognised_reasons_fail_loudly_instead_of_reporting_nothing_to_do() -> None:
    """这条是整个流程最重要的静默失败防线。

    亚马逊哪天改了那句文案（或者把全角冒号换成半角、或者在文字外面包个徽章让
    精确匹配失效），候选就会变成 0 → 空计划 → 任务成功。于是**每家店永远报绿**，
    而一条都没提交，没有任何人会发现。
    """

    drifted = [
        row("GPSR - Warning and safety information", "B0HJG2CJ5F"),
        row("GPSR﹕警告和安全信息", "B0GXZ1ZZLT"),
    ]
    result = select_actionable(drifted)
    assert result.candidates == ()
    # 不阻断（运营决定），但信号必须留下来往上报。
    assert result.unknown_reasons == {
        "GPSR - Warning and safety information",
        "GPSR﹕警告和安全信息",
    }


@pytest.mark.parametrize(
    "drifted",
    [
        "GPSR： 警告和安全信息",      # 冒号后多一个空格
        "GPSR:警告和安全信息",         # 全角冒号变半角
        "GPSR：警告和安全信息​",      # 末尾一个零宽空格（\s 匹配不到）
        "GPSR：警告和安全信息。",      # 末尾多一个句号
    ],
    ids=["extra-space", "halfwidth-colon", "zero-width", "period-suffix"],
)
def test_drift_confined_to_the_target_string_is_reported_not_swallowed(
    drifted: str,
) -> None:
    """⚠️ 这是本流程最危险的静默失败，而且**已经没有任何代码会因此停下**。

    亚马逊只改目标那一句时，增值税/负责人那几句仍然合法，所以「有没有认识的原因」
    这种判据永远不会响；候选静默变 0、任务报成功，而页面上明明有活儿。

    运营 2026-09-16 明确要求不因未知原因阻断，所以这里只钉**信号不能丢**：
    那句漂移过的文案必须出现在 ``unknown_reasons`` 里，一路报到运行事件和
    飞书卡片上。它只在有人真的去看的时候才有用——这就是去掉阀门换来的代价。
    """

    page = [
        row("欧洲增值税登记要求", None, cells=[
            "欧洲增值税登记要求", "2026年9月11日", "-", "不适用",
            "已发送警告", "无影响", "",
        ]),
        row(drifted, "B0HJG2CJ5F"),
    ]
    result = select_actionable(page)
    assert result.candidates == ()
    assert drifted.strip() in {value.strip() for value in result.unknown_reasons}


def test_a_clean_store_reports_no_unknown_reasons() -> None:
    """正常页面不能天天报「有未知原因」，否则这个信号会被无视。"""

    assert select_actionable(live_page()).unknown_reasons == frozenset()


@pytest.mark.parametrize(
    "prefix", ["\n", "\n\n", "  \n "], ids=["one", "two", "indented"]
)
def test_a_blank_first_line_in_the_reason_cell_does_not_hide_the_row(prefix: str) -> None:
    """一个前置 <br>（或 innerText 退化成带缩进的 textContent）就会让第 0 行是空白。

    取「第 0 行」的话，这一行既不进 reasons_seen 也不计 drift——**静默消失**，
    而页面上它明明是一条该提交的记录。
    """

    result = select_actionable([row(prefix + TARGET_REASON, "B0HJG2CJ5F")])
    assert [item.asin for item in result.candidates] == ["B0HJG2CJ5F"]


@pytest.mark.parametrize(
    "action",
    ["商品 已移除", "商品\n已移除", "商品 已移除"],
    ids=["space", "newline", "nbsp"],
)
def test_whitespace_inside_the_removed_phrase_still_excludes(action: str) -> None:
    """比对前两边都要**删掉**空白，不是压成一个空格。

    ``_normalize`` 把空白压成一个空格，而「商品已移除」里一个空白都没有——所以
    走 normalize 对「商品 已移除」照样匹配不上。用错函数等于没加这道闸。
    亚马逊把状态值包进 kat-badge 这类块级元素，innerText 就会在短语中间插换行。
    """

    result = select_actionable([row(TARGET_REASON, "B0HJG2CJ5F", action=action)])
    assert result.candidates == ()
    assert result.already_removed == 1


def test_one_dirty_copy_vetoes_the_whole_asin() -> None:
    """「已下架」是**商品**的属性，不是 DOM 节点的属性。

    三份响应式副本里只要有一份说它下架了，整个 ASIN 就不许提交。否则最宽松的
    那一份副本说了算——一次不可逆的假合规声明就此发出。副本顺序不能影响结论。
    """

    clean = row(TARGET_REASON, "B0GWCRRDZS")
    dirty = row(TARGET_REASON, "B0GWCRRDZS", action=ACTION_ALREADY_REMOVED)
    for order in ([dirty, clean, clean], [clean, clean, dirty], [clean, dirty, clean]):
        result = select_actionable(order)
        assert result.candidates == (), order.index(dirty)
        assert result.already_removed == 1
        # Selection 自身必须自洽：候选集合与已下架集合不相交。
        assert "B0GWCRRDZS" not in {item.asin for item in result.candidates}


def test_a_vetoing_copy_counts_even_when_that_copy_is_itself_unusable() -> None:
    """带标记的那份副本可能自己就先被别的闸挡掉（列数漂移、组标题、id 不对）。

    「已下架」的采集必须跑在**所有资格闸之前**，否则那份副本被跳过后，剩下两份
    干净的副本照样会把这个 ASIN 送去提交，而且 already_removed 会是 0——连矛盾
    的痕迹都不留。
    """

    clean = row(TARGET_REASON, "B0GWCRRDZS")
    for unusable in (
        row(TARGET_REASON, "B0GWCRRDZS", action=ACTION_ALREADY_REMOVED,
            cells=[TARGET_REASON, "d", "p", "r", ACTION_ALREADY_REMOVED, "i"]),
        row(TARGET_REASON, "B0GWCRRDZS", action=ACTION_ALREADY_REMOVED, header=True),
    ):
        result = select_actionable([unusable, clean, clean])
        assert result.candidates == ()
        assert result.already_removed == 1


def test_a_genuinely_clean_store_is_not_mistaken_for_drift() -> None:
    """没有待办和页面变了必须分得开，否则上面那道闸会天天误报。"""

    clean = [
        row("欧洲增值税登记要求", None, cells=[
            "欧洲增值税登记要求", "2026年9月11日", "-", "不适用",
            "已发送警告", "无影响", "",
        ]),
        row("GPSR：制造商详细联系信息", "B0HJFVKQHM", nested=True),
    ]
    result = select_actionable(clean)
    assert result.candidates == ()
    assert result.reasons_seen & KNOWN_REASONS


def test_an_empty_list_is_not_a_contract_violation() -> None:
    """筛选之后一行都没有是正常的，别把它当成结构漂移。"""

    result = select_actionable([])
    assert result.candidates == ()
    assert result.rows_read == 0


def test_rows_without_any_reason_text_do_not_trigger_the_drift_gate() -> None:
    """EPR 的组内行实测就是没有原因文案，不能因此判整站失败。"""

    blank = row("", None, cells=["", "", "某个商品", "", "", "", "查看商品信息"])
    known = row("EPR: 德国 WEEE-小型设备", "21862491", header=True)
    result = select_actionable([blank, known])
    assert result.candidates == ()


def test_the_reason_strings_keep_amazons_own_inconsistent_punctuation() -> None:
    """亚马逊自己的标点就不一致，白名单必须照抄，不能"顺手统一"。

    抄错一个字符，这条原因就会被当成未知 → 触发契约违约 → 整站判失败。
    """

    assert "GPSR：警告和安全信息" in KNOWN_REASONS       # 全角冒号，无空格
    assert "GPSR： 负责人详细联系信息" in KNOWN_REASONS  # 全角冒号，有空格
    assert "GPSR：制造商详细联系信息" in KNOWN_REASONS   # 全角冒号，无空格
    assert "EPR: 德国 WEEE-小型设备" in KNOWN_REASONS    # 半角冒号 + 空格


def test_the_title_is_read_without_the_asin_and_sku_lines() -> None:
    """运行事件里要放人看得懂的标题，不是整格原文。"""

    result = select_actionable([row(TARGET_REASON, "B0HJG2CJ5F")])
    title = result.candidates[0].title
    assert title.startswith("Highland-Kuh-Keksformen")
    assert "ASIN" not in title and "SKU" not in title


# --------------------------------------------------------------------------
# 折叠组标题的「N 受影响的…」后缀（真机原文，2026-09-16）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        # ⚠️ 三份响应式副本的分隔方式**不一样**，真机原样抄下来的：
        "GPSR：制造商详细联系信息3 受影响的 ASIN3 受影响的 SKU",   # 完全没有分隔符
        "GPSR：制造商详细联系信息 3 受影响的 ASIN 3 受影响的 SKU",  # 只有空格
        "GPSR：制造商详细联系信息\n\n3 受影响的 ASIN\n3 受影响的 SKU",  # 换行
        "EPR: 德国 WEEE-小型设备2 受影响的 SKU",
        "GPSR： 负责人详细联系信息3 受影响的 ASIN3 受影响的 SKU",
    ],
    ids=["no-separator", "spaces", "newlines", "epr-sku-only", "responsible-person"],
)
def test_group_header_counts_do_not_look_like_an_unknown_reason(raw: str) -> None:
    """⚠️ 真机第一次空跑就踩到了：每次运行都报一次假的「未知原因」。

    「取第一行」只能去掉用换行分隔的那一份副本；另外两份把计数直接拼在文案后面，
    于是组标题被当成白名单之外的原因。运营去掉阻断阀门之后，这条提醒是目标文案
    漂移的**唯一**信号——天天误报就等于把它废掉。
    """

    result = select_actionable([row(raw, "21862491", header=True)])
    assert result.unknown_reasons == frozenset(), raw
    assert result.reasons_seen <= KNOWN_REASONS


def test_the_real_page_reports_no_unknown_reason() -> None:
    """整页复核：正常的德国站页面不该有任何未知原因。"""

    page = live_page() + [
        row("GPSR：制造商详细联系信息3 受影响的 ASIN3 受影响的 SKU", "21862491", header=True),
        row("EPR: 德国 WEEE-小型设备2 受影响的 SKU", "21862491", header=True),
    ]
    assert select_actionable(page).unknown_reasons == frozenset()


def test_a_genuinely_new_reason_still_gets_reported() -> None:
    """去后缀不能把真信号一起去掉。"""

    result = select_actionable(
        [row("GPSR：某个全新的合规要求2 受影响的 SKU", "21862491", header=True)]
    )
    assert result.unknown_reasons == {"GPSR：某个全新的合规要求"}


def test_one_removed_asin_does_not_exempt_the_whole_fallback_gate() -> None:
    """⚠️ 豁免的范围只能是**真的已下架的那些**，不是全体。

    原来的判据写成「只要 target_asins & removed 非空就不检查」。于是这一幕会静默
    通过：11 个目标里 1 个已下架、另外 10 个因为亚马逊改了「采取的操作」文案
    （或插了一列）全部落进 drift_rows —— 候选为空、闸不响、站点报「没有待办」、
    run 报成功、飞书绿卡。10 个仍有移除风险的商品就这么消失了。
    """

    removed = row(TARGET_REASON, "B0GWCRRDZS", action=ACTION_ALREADY_REMOVED)
    blocked = [
        row(TARGET_REASON, asin, cells=[TARGET_REASON] + ["x"] * 5)
        for asin in ("B0HJG2CJ5F", "B0GXZ1ZZLT")
    ]

    with pytest.raises(GpsrContractError, match="一条都无法处理"):
        select_actionable([removed, *blocked])


def test_a_site_whose_only_target_is_already_removed_is_not_a_contract_error() -> None:
    """反向验证：真的只剩已下架条目时，那是关于店铺的事实，不是故障。"""

    result = select_actionable(
        [row(TARGET_REASON, "B0GWCRRDZS", action=ACTION_ALREADY_REMOVED)]
    )
    assert result.candidates == ()
    assert result.already_removed == 1
