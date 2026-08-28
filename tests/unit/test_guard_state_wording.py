"""「已发出，平台尚未显示」必须读得出「钱已经出去了」。

操作员看到原来的「已提交，等待平台审核」时问的是「是不是在等我审核」——完全合理，
因为同一份状态表里 WAITING_APPROVAL 就叫「待审核」，而那个**确实**在等他。两者读起来
像同一类东西，实际相反：一个等人批准，一个是钱已经请求出去、只是平台还没显示。

这类误读的代价不是困惑而已：以为「还没发出去」的人可能会去手动再点一次提现。
"""

from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUARD_STATES = ("UNCERTAIN", "UNCERTAIN_FINANCIAL")


def _sources() -> dict[str, str]:
    return {
        name: (PROJECT_ROOT / path).read_text(encoding="utf-8")
        for name, path in {
            "web": "src/ziniao_automation/web.py",
            "feishu": "src/ziniao_automation/notifications/feishu.py",
            "database": "src/ziniao_automation/notifications/database.py",
            "run_detail": "src/ziniao_automation/templates/run_detail.html",
        }.items()
    }


@pytest.mark.parametrize("name", ["web", "feishu", "database"])
def test_no_surface_says_the_platform_is_reviewing_it(name: str) -> None:
    """没有人在审核。用「审核」会把它和真正等人批的 WAITING_APPROVAL 混为一谈。"""

    assert "等待平台审核" not in _sources()[name]


def test_every_surface_uses_the_same_wording() -> None:
    """网页、飞书卡片、通知文案说同一句话，否则跨渠道对不上号。"""

    for name in ("web", "feishu", "database"):
        assert "已发出，平台尚未显示" in _sources()[name], name


def test_the_run_page_explains_the_state_not_just_the_buttons() -> None:
    """原来的说明只讲了三个按钮各自干什么，从没说过这个状态本身是什么意思。

    于是操作员看得懂「只回读，不重提」，却仍然不知道钱到底发出去没有。
    """

    note = _sources()["run_detail"]
    assert "不是在等你审核" in note
    assert "真的被点下去" in note
    assert "不会自己变好" in note, "必须写明它不会自动收尾，否则操作员会一直等"
    assert "永不重新提交" in note
    # 收尾那一步要指出来，否则排期会一直被这条 run 卡住。
    assert "资金记录已裁定完，结束这个任务" in note
