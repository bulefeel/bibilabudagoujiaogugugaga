"""提现按钮前的满意度问卷弹窗：只关认得出的，绝不乱点。

这段代码插在「点击请求付款」的前一刻，所以它的安全边界比功能更重要：

- 认不出的弹窗**一个按钮都不碰**——让后面「付款按钮必须唯一且可点」的既有
  判据去响亮失败，而不是为了清场去点一个身份不明的按钮；
- 问卷里的评分/提交控件永远不点，只点关闭或「以后再提醒我」；
- 关了但没关掉 → DomContractError，不带着遮挡继续走。

⚠️ 中文判据是推断值（原始弹窗文本没留下证据，0.6.3 未发布版里它们被写成了
``???``），真机见到后要把原文抄回 ``_survey_markers``。这里钉的是行为边界，
不是具体文案。
"""

from __future__ import annotations

import re

import pytest

from ziniao_automation.workflows.amazon_disbursement.page import (
    AmazonPaymentsPage,
    DomContractError,
)


class Button:
    def __init__(self, label: str = "", visible: bool = True) -> None:
        self.label = label
        self.visible = visible
        self.clicks = 0
        self.on_click = None

    async def is_visible(self) -> bool:
        return self.visible

    async def click(self, no_wait_after: bool = False) -> None:
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()


class ButtonList:
    def __init__(self, items: list[Button]) -> None:
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    @property
    def first(self) -> Button:
        return self.items[0]


class Dialog:
    def __init__(
        self,
        text: str,
        *,
        close_buttons: list[Button] | None = None,
        other_buttons: list[Button] | None = None,
        closes_on_click: bool = True,
    ) -> None:
        self.text = text
        self.visible = True
        self.close_buttons = close_buttons or []
        # 评分、提交、自由文本旁边的按钮——永远不许被点。
        self.other_buttons = other_buttons or []
        if closes_on_click:
            for button in self.close_buttons:
                button.on_click = lambda: setattr(self, "visible", False)

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        return self.text

    def locator(self, selector: str) -> ButtonList:
        assert "关闭" in selector or "Close" in selector
        return ButtonList([b for b in self.close_buttons if not b.label])

    def get_by_text(self, pattern: re.Pattern) -> ButtonList:
        matched = [
            b
            for b in self.close_buttons + self.other_buttons
            if b.label and pattern.search(b.label)
        ]
        return ButtonList(matched)


class Page:
    def __init__(self, dialogs: list[Dialog]) -> None:
        self.dialogs = dialogs

    def locator(self, selector: str):
        class Dialogs:
            def __init__(self, items):
                self.items = items

            async def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        return Dialogs(self.dialogs)

    async def wait_for_timeout(self, ms: int) -> None:
        return None


def subject() -> AmazonPaymentsPage:
    # _dismiss_feedback_survey 只用到类属性与模块级 logger，不需要构造依赖。
    return object.__new__(AmazonPaymentsPage)


CHINESE_SURVEY = "您愿意参与满意度调查，为卖家平台评分吗？"


async def test_a_recognised_survey_is_closed_via_its_close_button() -> None:
    close = Button()
    rating = Button(label="提交")
    dialog = Dialog(CHINESE_SURVEY, close_buttons=[close], other_buttons=[rating])

    await subject()._dismiss_feedback_survey(Page([dialog]))

    assert close.clicks == 1
    assert rating.clicks == 0, "问卷的提交/评分控件绝不许被点"
    assert dialog.visible is False


async def test_an_unrecognised_dialog_is_left_completely_alone() -> None:
    """付款确认弹窗（或任何认不出的弹窗）一个按钮都不碰。"""

    close = Button()
    confirm = Button(label="确认")
    dialog = Dialog("确认请求付款", close_buttons=[close], other_buttons=[confirm])

    await subject()._dismiss_feedback_survey(Page([dialog]))

    assert close.clicks == 0
    assert confirm.clicks == 0
    assert dialog.visible is True


async def test_the_remind_me_later_fallback_is_used_without_a_close_button() -> None:
    remind = Button(label="以后再提醒我")
    submit = Button(label="提交反馈")
    dialog = Dialog(CHINESE_SURVEY, other_buttons=[remind, submit])
    remind.on_click = lambda: setattr(dialog, "visible", False)

    await subject()._dismiss_feedback_survey(Page([dialog]))

    assert remind.clicks == 1
    assert submit.clicks == 0


async def test_a_survey_without_a_recognised_dismissal_fails_loudly() -> None:
    """有问卷、没有认得出的关闭途径 → 响亮失败，不猜按钮。"""

    submit = Button(label="提交")
    dialog = Dialog(CHINESE_SURVEY, other_buttons=[submit])

    with pytest.raises(DomContractError):
        await subject()._dismiss_feedback_survey(Page([dialog]))
    assert submit.clicks == 0


async def test_a_survey_that_stays_visible_after_closing_fails_loudly() -> None:
    """点了关闭但弹窗还在 → 不带着遮挡去点付款按钮。"""

    close = Button()
    dialog = Dialog(CHINESE_SURVEY, close_buttons=[close], closes_on_click=False)

    with pytest.raises(DomContractError):
        await subject()._dismiss_feedback_survey(Page([dialog]))
    assert close.clicks == 1


async def test_a_page_without_locator_support_is_a_no_op() -> None:
    await subject()._dismiss_feedback_survey(object())
