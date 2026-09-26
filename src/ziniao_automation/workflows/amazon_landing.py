"""把页面**送到**某个 Seller Central 业务页,或者说清楚为什么送不到。

三条流程打开自己那张业务页时,撞到的是同一串岔路:会话掉了要登录、多站点账号被
落到账户/站点选择页、选完站还要再登一次该站点。每条流程各写一遍的代价已经付过
四次了(见 memory 里 business-page-allowlist-recurrence),所以这里只写一遍。

## 落地判据(顺序是承重的)

1. **先看登录,再看路径**。登录页同样不在业务页路径上,顺序反了就会把「会话过期」
   报成「这家店没开这个站点」,操作员去查一个不存在的开店问题。
2. **只比 path,不拿整个 URL 做子串匹配**。切换页长成
   ``/account-switcher?returnTo=/feedback-manager/index.html`` —— 目标路径明晃晃地
   出现在查询参数里,子串匹配会把切换页当成「已经到了」,然后去一张空表上找活儿。
3. 选站**只是换个视图,不碰凭据**;选不掉就如实说选不掉,绝不改口说「没开这个站点」。
4. 选完之后**重新打开一次原始 URL** —— ``returnTo`` 只带路径,不带查询串
   (GPSR 的 ``?t=regulatory-compliance`` 就在那里),不重开会读到一张没筛选的页。
5. 重开之后再判一次;又落回选择页就**不点第二次**,免得绕圈。

## 出口

``ok`` 只在**页面真的落在 expected_path 上**时为真。其余一律有名有姓:

* ``auth_required`` —— 停在 ``/ap/*``,登录助手也没走通。交给人工验证那条路径。
* ``marketplace_not_selected`` —— 停在账户/站点选择页,而且自己选不掉;
  ``switch_reason`` 说明是哪一种(没有整串精确匹配、不唯一、被挡住、列表没加载……)。
  ⚠️ 这**不等于**「这家店没开这个站点」:2026-09-18 正香-张刚CA 实测,切换页上
  「德國」在列且不是「待註冊」。没读到证据就不许下那个断言。
* ``marketplace_unavailable`` —— 落在别的地方。同样只陈述看见了什么。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import urlparse

from .amazon_account_switcher import AmazonAccountSwitcher
from .amazon_login import AmazonLoginAdvancer


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LandingOutcome:
    """到没到目标页,没到是因为什么。"""

    ok: bool
    reason: str = ""
    # 选站失败时的具体原因(``SwitchOutcome.reason``),进事件 details。
    switch_reason: str = ""
    landed_url: str = ""
    # ⚠️ 登录助手可能把登录收在**另一个标签**里(亚马逊会这么干,并故意把原标签留在
    # ``/ap/signin``)。后续抓取必须用这个 Page,否则会去读那个被遗弃的登录页 ——
    # 那上面一行都匹配不到,结果就成了「这家店没有待办」。
    page: Any = None


class AmazonLanding:
    """打开业务页的那几步。每条流程自己再等自己的表格/骨架。"""

    def __init__(
        self,
        *,
        settle_seconds: float = 2.0,
        login_advancer: AmazonLoginAdvancer | None = None,
        account_switcher: AmazonAccountSwitcher | None = None,
    ) -> None:
        self.settle_seconds = settle_seconds
        self._login_advancer = login_advancer or AmazonLoginAdvancer()
        self._switcher = account_switcher or AmazonAccountSwitcher()

    async def land_on(
        self, page: Any, *, url: str, expected_path: str, domain: str
    ) -> LandingOutcome:
        await self._goto(page, url)
        page, landed = await self._advance_login_if_needed(page, domain)

        verdict = self._classify(landed, expected_path)
        if verdict == "ok":
            return LandingOutcome(ok=True, landed_url=landed, page=page)
        if verdict == "auth":
            return LandingOutcome(
                ok=False, reason="auth_required", landed_url=landed, page=page
            )
        if verdict != "switcher":
            return LandingOutcome(
                ok=False,
                reason="marketplace_unavailable",
                landed_url=landed,
                page=page,
            )

        switched = await self._switcher.select_marketplace(
            page, domain, expected_path=expected_path
        )
        if switched.reason == "auth_required":
            # 站点其实选上了,是亚马逊要求该站点自己的登录(每站 assoc_handle 不同)。
            # 这正是登录助手的活:试一次。不试的话 AUTO 运行会停 30 分钟等人,
            # 还占着唯一的队列 worker。
            page, landed = await self._advance_login_if_needed(page, domain)
            again = self._classify(landed, expected_path)
            if again == "auth":
                return LandingOutcome(
                    ok=False, reason="auth_required", landed_url=landed, page=page
                )
            if again == "switcher":
                return self._not_selected(page, landed, "bounced_back")
        elif not switched.ok:
            return self._not_selected(
                page, switched.landed_url or landed, switched.reason
            )

        # 选完重开一次原始 URL:``returnTo`` 不带查询串。
        await self._goto(page, url)
        landed = str(getattr(page, "url", "") or "")
        verdict = self._classify(landed, expected_path)
        if verdict == "auth":
            page, landed = await self._advance_login_if_needed(page, domain)
            verdict = self._classify(landed, expected_path)
        if verdict == "ok":
            return LandingOutcome(ok=True, landed_url=landed, page=page)
        if verdict == "auth":
            return LandingOutcome(
                ok=False, reason="auth_required", landed_url=landed, page=page
            )
        if verdict == "switcher":
            # 选定了又被弹回选择页:不再点第二次。
            return self._not_selected(page, landed, "bounced_back")
        return LandingOutcome(
            ok=False, reason="marketplace_unavailable", landed_url=landed, page=page
        )

    # ------------------------------------------------------------------
    def _classify(self, landed: str, expected_path: str) -> str:
        """⚠️ 先看登录、再看选择页、最后才比路径。只比 path,不做子串匹配。"""

        path = urlparse(landed).path.rstrip("/")
        if path.startswith("/ap/") or path == "/ap":
            return "auth"
        if self._switcher.is_switcher_url(landed):
            return "switcher"
        return "ok" if path == expected_path.rstrip("/") else "other"

    def _not_selected(self, page: Any, landed: str, switch_reason: str) -> LandingOutcome:
        logger.info("landing: marketplace_not_selected reason=%s", switch_reason)
        return LandingOutcome(
            ok=False,
            reason="marketplace_not_selected",
            switch_reason=switch_reason,
            landed_url=landed,
            page=page,
        )

    async def _goto(self, page: Any, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(int(max(self.settle_seconds, 0.05) * 1000))

    async def _advance_login_if_needed(self, page: Any, domain: str) -> tuple[Any, str]:
        """落在登录页时让现成的登录助手去点按钮。**凭据全程不经过我们。**

        助手可能把登录收在另一个标签里,所以返回值要带上新的 Page。
        """

        landed = str(getattr(page, "url", "") or "")
        lowered = landed.lower()
        if "/ap/signin" not in lowered and "/ap/mfa" not in lowered:
            return page, landed
        resolved = await self._login_advancer.advance_or_raise(
            page, expected_host=domain
        )
        if resolved is None:
            # 助手用尽预算也没走通(非托管 Passkey、CAPTCHA、候选不唯一……)。
            # 交给人工,绝不猜 —— 猜错会给卖家真实手机发消息或打电话。
            return page, landed
        page = resolved
        await page.wait_for_timeout(int(max(self.settle_seconds, 0.05) * 1000))
        return page, str(getattr(page, "url", "") or "")


__all__ = ["AmazonLanding", "LandingOutcome"]
