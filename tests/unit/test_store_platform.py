"""店铺的平台归属读自紫鸟同步的 ``raw_profile``（2026-09-26）。

紫鸟公司账号下混着亚马逊、沃尔玛、TikTok 店铺，三条流程只对亚马逊生效。同步接口
（官方文档 getBrowserList）给的 ``platform_id`` / ``platform_name`` 一直原样躺在
``raw_profile`` 里，页面却只显示店铺名，操作员分不清哪些是亚马逊。

下面的样例值全部来自线上库：34 家亚马逊店 ``platform_id`` 都是 1，唯一一家
TikTok Shop 是 92。
"""

from __future__ import annotations

import pytest

from ziniao_automation.models import (
    AMAZON_PLATFORM_ID,
    UNKNOWN_PLATFORM_LABEL,
    Store,
    StorePlatform,
)


AMAZON_CA = {"browserOauth": "x", "platform_id": 1, "platform_name": "亚马逊-加拿大", "siteId": 2}
TIKTOK = {
    "browserOauth": "y",
    "platform_id": 92,
    "platform_name": "TikTok Shop-全球跨境(自运营)(新)",
    "siteId": 458,
}


def test_an_amazon_record_is_split_into_family_and_region() -> None:
    platform = StorePlatform.from_raw_profile(AMAZON_CA)

    assert platform.known is True
    assert platform.is_amazon is True
    assert platform.id == AMAZON_PLATFORM_ID
    assert platform.label == "亚马逊"
    assert platform.region == "加拿大"


def test_a_tiktok_record_is_not_amazon_even_though_its_name_has_a_dash_inside() -> None:
    """名字里「-」后面还有括号和多段文字，只按第一个「-」切。"""

    platform = StorePlatform.from_raw_profile(TIKTOK)

    assert platform.known is True
    assert platform.is_amazon is False
    assert platform.label == "TikTok Shop"
    assert platform.region == "全球跨境(自运营)(新)"


@pytest.mark.parametrize("raw", [None, {}, {"browserOauth": "z"}, "not-a-dict"])
def test_a_record_without_platform_fields_is_unknown_not_non_amazon(raw) -> None:
    """⚠️ 「不知道」和「不是亚马逊」是两回事：老版本紫鸟不带这两个字段，重新同步
    前页面只能说「平台未知」，不能把一家亚马逊店标成别的平台。"""

    platform = StorePlatform.from_raw_profile(raw)

    assert platform.known is False
    assert platform.is_amazon is False
    assert platform.label == UNKNOWN_PLATFORM_LABEL
    assert platform.region == ""


def test_the_family_text_backs_up_the_numeric_id() -> None:
    """id 对照表哪天变了，页面上写着「亚马逊」的店也不能被当成别的平台。"""

    by_name_only = StorePlatform.from_raw_profile({"platform_id": 7, "platform_name": "亚马逊-美国"})
    by_id_only = StorePlatform.from_raw_profile({"platform_id": "1", "platform_name": ""})
    neither = StorePlatform.from_raw_profile({"platform_id": 7, "platform_name": "沃尔玛-美国"})

    assert by_name_only.is_amazon is True
    assert by_id_only.is_amazon is True
    assert neither.is_amazon is False and neither.label == "沃尔玛"


def test_a_platform_name_without_a_dash_has_no_region() -> None:
    platform = StorePlatform.from_raw_profile({"platform_id": 5, "platform_name": "沃尔玛"})

    assert platform.label == "沃尔玛"
    assert platform.region == ""


def test_the_store_model_reads_its_platform_from_the_latest_raw_profile() -> None:
    """不另存列：每次同步整体覆盖 raw_profile，属性跟着变。"""

    store = Store(name="n", selector_type="oauth", selector_value="v", raw_profile=dict(AMAZON_CA))
    assert store.platform.is_amazon is True

    store.raw_profile = dict(TIKTOK)
    assert store.platform.is_amazon is False and store.platform.label == "TikTok Shop"


@pytest.mark.parametrize("raw", [
    {"platform_id": "1", "platform_name": ""},
    {"platform_name": "Amazon-US"},
])
def test_amazon_labels_are_grouped_under_the_default_ledger_filter(raw) -> None:
    platform = StorePlatform.from_raw_profile(raw)
    assert platform.is_amazon
    assert platform.label == "亚马逊"
