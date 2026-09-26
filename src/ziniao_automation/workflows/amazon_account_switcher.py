"""在 Seller Central 的账户/站点切换页上选定目标站点。

登录成功之后，亚马逊会把多站点账号的请求落到
``/account-switcher/default/merchantMarketplace?returnTo=<原路径>``，
让人挑一个站点。三条流程都会撞到这一页，原本一律当作「这家店没开这个站点」跳过
——那对多站点账号是一句假话，而且把一个纯粹的视图切换变成了每店一次的人工活。

⚠️ **这和登录助手里那条「账户选择 → 转人工」是两件事，别混。**
那条防的是**在登录页上选用哪个账号登录**，属于凭据决定；这一页发生在登录之后，
选的是「打开哪个站点的后台」，不碰任何凭据。

## 这一页是两步（2026-09-18 在正香-张刚CA 的德国站实地量出来的）

1. 点国家 → 那颗按钮拿到 ``…-account-selected`` 类（对比探针：点前 0 个、
   点后 1 个，旁边还多一个 ``kat-icon.…-account-selected-icon``）。**页面不导航。**
2. 点右下角的确认按钮 → 才真的切站并沿 ``returnTo`` 回到原路径。

第一版只做了第 1 步，于是「点了、选中态也变了、页面就是不动」。
确认按钮是 ``<kat-button>``（Katal 自定义元素），普通 ``button`` 选择器
**根本看不见它**——这是那次卡住的直接原因。

## 判据

* **国家**只能靠按钮文案：20 颗站点按钮的属性完全一样（只有 ``data-v-6b3157d9``
  和同一个 class），而页面上唯一的 marketplace id（``A1PA6795UKMFR9``）只出现在
  ``ue_mid`` / ``ObfuscatedMarketplaceId`` 里——那是**当前页面自己**的站点，
  不是逐行的；「德國」那颗按钮往上六层祖先一个 id 都没有。
  既然只能用文案，就必须 fail-closed：**整串精确相等 + 命中必须唯一**，
  否则一下都不点。「日本 (待註冊)」因此天然被排除，那正好是「账号根本没开这个
  站点」和「开了只是没选定」的分界。
* **确认按钮**用 ``data-test="confirm-selection"``，语言无关，不碰
  ``label="選取帳戶"`` 那套繁/简/英。
* 两步各有自己的**结果判据**：第一步盯选中态，第二步盯落地路径。
  没有任何一步是「我点了就算数」。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse


logger = logging.getLogger(__name__)


ACCOUNT_SWITCHER_PATH_PREFIX = "/account-switcher"

# 站点按钮的选择器。语义 class，不是构建期哈希（2026-09-18 实地读的）。
MARKETPLACE_BUTTON_SELECTOR = "button.full-page-account-switcher-account-details"

# 选中之后按钮会拿到这个 class。这是第一步的结果判据。
SELECTED_CLASS = "full-page-account-switcher-account-selected"

# 第二步的确认按钮。``data-test`` 语言无关，优于 ``label``。
CONFIRM_BUTTON_SELECTOR = 'kat-button[data-test="confirm-selection"]'

# 每个站点可接受的**完整**按钮文案。
#
# 按**域名**键控，因为调用方手上拿到的就是域名（``open_policies(page, domain)``），
# 这样既不必改签名，也不必再维护一份 code↔domain 的映射。
#
# ⚠️ 繁體那一列是 2026-09-18 在正香的德国站切换页上**实地读到**的；简体与英文是
# 按 Seller Central 的界面语言补的**推断值**。推断值在这里是安全的：匹配是整串
# 相等 + 必须唯一，推错了只会「匹配不到 → 跳过」，绝不会点到别的国家。
# 实地遇到新的写法时把它加进来，别改成模糊匹配。
MARKETPLACE_NAMES: Mapping[str, frozenset[str]] = {
    "sellercentral.amazon.de": frozenset({"德國", "德国", "Germany"}),
    "sellercentral.amazon.ca": frozenset({"加拿大", "Canada"}),
    "sellercentral.amazon.co.uk": frozenset({"英國", "英国", "United Kingdom"}),
    "sellercentral.amazon.com.au": frozenset(
        {"澳洲", "澳大利亚", "澳大利亞", "Australia"}
    ),
}


# ⚠️ JS 只**抓**不**判**。「整串精确相等 + 命中必须唯一」是整个模块唯一的 fail-closed
# 闸,放在 JS 里的话测试替身根本不会执行它(2026-09-23 自审抓到:它从来没被测过)。
# 所以 JS 只回按钮文字,判断在 Python 的 ``pick_marketplace`` 里做。

# ① 全部站点按钮的文字(空白压成单个空格)。
_LIST_BUTTONS = r"""
(selector) => [...document.querySelectorAll(selector)]
  .map(b => (b.innerText || '').split(/\s+/).filter(Boolean).join(' '))
"""

# ② 第 index 颗按钮此刻的文字、中心坐标,以及那个坐标上最上层的元素是不是它。
# 只抓事实;「文字还是不是目标」「是不是被挡住」由 Python 判(``_click_target``)。
_RECT_AT = r"""
(payload) => {
  const squeeze = s => (s || '').split(/\s+/).filter(Boolean).join(' ');
  const button = [...document.querySelectorAll(payload.selector)][payload.index];
  if (!button) return {text: null};
  button.scrollIntoView({block: "center"});
  const r = button.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const top = document.elementFromPoint(x, y);
  return {
    text: squeeze(button.innerText),
    x: x,
    y: y,
    onTop: !!top && (top === button || button.contains(top)),
  };
}
"""

# ③ 此刻处于选中态的按钮的文字。点完之后必须**只有目标那一颗** —— 上次选中的别国
# 没被取消的话,确认会切到错的国家。
_SELECTED_TEXTS = r"""
(payload) => [...document.querySelectorAll(payload.selector)]
  .filter(b => b.classList.contains(payload.selectedClass))
  .map(b => (b.innerText || '').split(/\s+/).filter(Boolean).join(' '))
"""

# ④ 确认按钮的坐标。按坐标做真实鼠标点击：kat-* 是自定义元素，
# locator 的可见性过滤和 shadow DOM 都不好打交道。
_CONFIRM_RECT = r"""
(selector) => {
  const buttons = [...document.querySelectorAll(selector)].filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (buttons.length !== 1) {
    return {ok: false, reason: "confirm_not_unique", count: buttons.length};
  }
  const button = buttons[0];
  if (button.hasAttribute("disabled")) return {ok: false, reason: "confirm_disabled"};
  button.scrollIntoView({block: "center"});
  const r = button.getBoundingClientRect();
  return {ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2};
}
"""


def pick_marketplace(texts: list[str], names: frozenset[str]) -> tuple[int | None, str]:
    """在按钮文字列表里挑出目标国家:**整串精确相等**,且**恰好一个**。

    返回 ``(下标, "")``;挑不出就返回 ``(None, 原因)``,调用方一下都不点。

    * 「日本 (待註冊)」带后缀,整串精确匹配天然排除 —— 那正是「账号没开这个站点」
      和「开了只是没选定」的分界。
    * 多账号卖家的切换页里同一个国家会出现两次 → ``ambiguous``,点哪个都可能错。
    """

    hits = [index for index, text in enumerate(texts) if text in names]
    if not hits:
        return None, "not_offered"
    if len(hits) > 1:
        return None, "ambiguous"
    return hits[0], ""


def _click_target(rect: Mapping[str, Any], target_text: str) -> tuple[bool, str]:
    """点之前的最后一道闸:那一格上**现在**还是目标国家,而且没被别的元素盖住。

    * 两次 evaluate 之间列表可能重排,拿着旧下标去点会点到别的国家 → ``list_changed``;
    * 坐标上最上层的不是这颗按钮(弹层、横幅)→ ``obstructed``。
    """

    if rect.get("text") != target_text:
        return False, "list_changed"
    if not rect.get("onTop"):
        return False, "obstructed"
    return True, ""


@dataclass(frozen=True, slots=True)
class SwitchOutcome:
    """切换站点的出口。``ok`` 只在**确认落地到目标路径**之后才为真。"""

    ok: bool
    reason: str = ""
    offered: int = 0
    landed_url: str = ""


class AmazonAccountSwitcher:
    def __init__(
        self,
        *,
        settle_seconds: float = 2.0,
        timeout_seconds: float = 45.0,
        names: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self.settle_seconds = settle_seconds
        self.timeout_seconds = timeout_seconds
        self._names = names or MARKETPLACE_NAMES

    @staticmethod
    def is_switcher_url(url: str) -> bool:
        path = urlparse(str(url or "")).path.rstrip("/")
        return path == ACCOUNT_SWITCHER_PATH_PREFIX or path.startswith(
            f"{ACCOUNT_SWITCHER_PATH_PREFIX}/"
        )

    @staticmethod
    def return_path(url: str) -> str | None:
        """切换页 URL 里 ``returnTo`` 指向的路径 —— 亚马逊自己写的「选完回哪」。

        没有、或者不是站内绝对路径,就返回 ``None``:调用方不该去猜。
        """

        values = parse_qs(urlparse(str(url or "")).query).get("returnTo") or []
        if len(values) != 1:
            return None
        target = urlparse(values[0])
        if target.scheme or target.netloc or not target.path.startswith("/"):
            return None
        return target.path

    async def select_marketplace(
        self, page: Any, domain: str, *, expected_path: str
    ) -> SwitchOutcome:
        """在切换页上选定 ``domain`` 对应的站点，并等它真的落回 ``expected_path``。

        出口（没有第四态，也没有「点了就算成」）：

        * ``ok=True`` / ``selected`` —— 已落地到 ``expected_path``。
        * ``list_not_loaded`` / ``not_offered`` / ``ambiguous`` / ``obstructed`` /
          ``list_changed`` / ``unknown_marketplace``
          —— **一下都没点**，页面原样留着，调用方照旧跳过该站点。
        * ``selection_not_registered`` —— 点了国家之后,选中态不是「只有目标那一颗」,
          **不点确认**(否则可能切到上次选中的别国)。
        * ``confirm_not_unique`` / ``confirm_disabled`` —— 选中了但找不到确认按钮。
        * ``timeout`` —— 确认点了但没等到落地。**不重试**：反复点一个没反应的
          按钮只会把问题埋得更深。
        * ``auth_required`` —— 确认之后被弹去登录页(``/ap/*``):立刻报出来交给
          登录/人工验证,而不是白等 45 秒再报成「停在选择页」。
        """

        names = self._names.get(str(domain).lower())
        if not names:
            return SwitchOutcome(ok=False, reason="unknown_marketplace")

        # ⚠️ 等列表加载完再判。登录之后落到这一页时,账户列表是异步到达的 ——
        # 只读一次会读到空表,于是判成「列表里没有这个国家」,而那是一句假话
        # (09-23 自审复核)。判据:连续两次读到**同一份非空**列表。
        texts: list[str] = []

        async def listed() -> bool:
            nonlocal texts
            current = list(
                await page.evaluate(_LIST_BUTTONS, MARKETPLACE_BUTTON_SELECTOR)
            )
            settled = bool(current) and current == texts
            texts = current
            return settled

        if not await self._poll(listed, page):
            return self._refused(page, domain, "list_not_loaded", len(texts))
        offered = len(texts)
        index, refusal = pick_marketplace(texts, names)
        if index is None:
            return self._refused(page, domain, refusal, offered)
        target_text = texts[index]

        rect = await page.evaluate(
            _RECT_AT, {"selector": MARKETPLACE_BUTTON_SELECTOR, "index": index}
        )
        clickable, refusal = _click_target(rect, target_text)
        if not clickable:
            return self._refused(page, domain, refusal, offered)
        located = rect

        # 第一步：点国家。它只是选中，页面不会导航。
        await page.mouse.click(located["x"], located["y"])
        selection = {"selector": MARKETPLACE_BUTTON_SELECTOR, "selectedClass": SELECTED_CLASS}

        async def only_target_selected() -> bool:
            return list(await page.evaluate(_SELECTED_TEXTS, selection)) == [target_text]

        if not await self._poll(only_target_selected, page):
            return self._refused(page, domain, "selection_not_registered", offered)

        # 第二步：点确认，这一下才真的切站。
        confirm = await page.evaluate(_CONFIRM_RECT, CONFIRM_BUTTON_SELECTOR)
        if not confirm.get("ok"):
            return self._refused(
                page, domain, str(confirm.get("reason") or "confirm_not_unique"), offered
            )
        await page.mouse.click(confirm["x"], confirm["y"])

        landed, reason = await self._wait_for_path(page, expected_path)
        if landed is None:
            return self._refused(page, domain, reason, offered)
        logger.info("account_switcher: domain=%s 已选定并落地", domain)
        return SwitchOutcome(
            ok=True, reason="selected", offered=offered, landed_url=landed
        )

    def _refused(
        self, page: Any, domain: str, reason: str, offered: int
    ) -> SwitchOutcome:
        logger.info(
            "account_switcher: domain=%s outcome=%s offered=%d", domain, reason, offered
        )
        return SwitchOutcome(
            ok=False,
            reason=reason,
            offered=offered,
            landed_url=str(getattr(page, "url", "") or ""),
        )

    async def _poll(self, check: Any, page: Any) -> bool:
        """轮询等一个布尔判据成立。⚠️ 不用固定 sleep 赌它好了。

        下限不能去掉：``settle_seconds=0`` 时 ``waited += step`` 永远加 0，
        这个循环就再也出不来了。
        """

        step = max(self.settle_seconds, 0.05)
        waited = 0.0
        while True:
            if await check():
                return True
            if waited >= self.timeout_seconds:
                return False
            await page.wait_for_timeout(int(step * 1000))
            waited += step

    async def _wait_for_path(
        self, page: Any, expected_path: str
    ) -> tuple[str | None, str]:
        """等页面落到 ``expected_path``。被弹去登录页就立刻停,不白等。"""

        target = expected_path.rstrip("/")
        bounced = False

        async def settled() -> bool:
            nonlocal bounced
            path = urlparse(str(getattr(page, "url", "") or "")).path.rstrip("/")
            if path.startswith("/ap/") or path == "/ap":
                bounced = True
                return True
            return path == target

        await self._poll(settled, page)
        current = str(getattr(page, "url", "") or "")
        if bounced:
            return None, "auth_required"
        if urlparse(current).path.rstrip("/") == target:
            return current, ""
        logger.warning(
            "account_switcher: 确认后未落地到 %s，仍在 %s", expected_path, current[:80]
        )
        return None, "timeout"


__all__ = [
    "ACCOUNT_SWITCHER_PATH_PREFIX",
    "pick_marketplace",
    "CONFIRM_BUTTON_SELECTOR",
    "MARKETPLACE_BUTTON_SELECTOR",
    "MARKETPLACE_NAMES",
    "SELECTED_CLASS",
    "AmazonAccountSwitcher",
    "SwitchOutcome",
]
