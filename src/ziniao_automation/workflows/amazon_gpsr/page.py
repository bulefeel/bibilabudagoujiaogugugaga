"""德国站监管合规性页的页面操作。

会动手的那半边（打开安全证明卡、勾选、保存）已经接上。保存那一下**不可撤销**，
它的每一条不变量都在同一个 JS tick 内复核，见 ``save_attestation``。

分工是刻意的，而且是被测试夹具逼出来的：``FixturePage`` 只有 ``locator()``，没有
页面级 ``evaluate``，而 Katal 的 shadow DOM 必须走页面级 ``evaluate`` 才读得到。
所以 **JS 只抓不判**，全部判断都在 ``config.select_actionable`` 那个纯函数里——
那是它们唯一能被覆盖的地方。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import urlparse

from ..amazon_landing import AmazonLanding
from ..amazon_login import AmazonLoginAdvancer
from ..errors import DomContractError
from ..amazon_account_switcher import AmazonAccountSwitcher
from .config import (
    ACTION_BUTTON_PREFIX,
    FILTER_CHIP_PATTERN,
    PRODUCT_POLICIES_PATH,
    GpsrDomContract,
    policies_url,
)


logger = logging.getLogger(__name__)


# 抓取器：照抄每一行的文本和按钮 id，一个判断都不做。
_SCRAPE_ROWS = """
(contract) => {
  const rows = [...document.querySelectorAll(contract.rows)];
  return rows.map(row => {
    const button = row.querySelector(contract.actionButton);
    return {
      cells: [...row.children].map(cell => cell.innerText || ""),
      buttonId: button ? button.id : null,
      inAccordionHeader: !!row.closest(contract.accordionHeader),
      inAccordionBody: !!row.closest(contract.accordionBody),
    };
  });
}
"""

_ROW_COUNT = """
(contract) => document.querySelectorAll(contract.rows).length
"""


# ---------------------------------------------------------------------------
# 会动手的那半边
# ---------------------------------------------------------------------------
# 面板定位：从「安全证明」卡或可见复选框往上走，找到最近的、自报 ASIN 的祖先。
# 容器本身没有稳定 class（全是构建期哈希），但这条推导是稳定的。
_PANEL_HELPERS = """
  const vis = el => el && el.offsetParent !== null;
  function panelFrom(anchor) {
    let node = anchor, hops = 0;
    while (node && node !== document.body && hops < 16) {
      if (/ASIN/.test(node.innerText || "")) return node;
      node = node.parentElement; hops++;
    }
    return null;
  }
  function asinsIn(panel) {
    return [...new Set((panel.innerText || "").match(/B0[A-Z0-9]{8}/g) || [])];
  }
"""

_PANEL_STATE = """
(contract) => {
  %s
  const cards = [...document.querySelectorAll(contract.attestationCard)].filter(vis);
  if (cards.length !== 1) return {ok: false, reason: "panel_not_unique", cards: cards.length};
  const card = cards[0];
  const panel = panelFrom(card);
  if (!panel) return {ok: false, reason: "panel_without_asin"};
  const asins = asinsIn(panel);
  if (asins.length !== 1) return {ok: false, reason: "panel_asin_ambiguous", asins};
  const label = card.querySelector(contract.cardStatus);
  return {
    ok: true,
    asin: asins[0],
    status: label ? (label.getAttribute("text") || "").trim() : "",
  };
}
""" % _PANEL_HELPERS

# ⚠️ 判断「侧栏是否开着」**不能**数「安全证明」卡：一钻进那张卡，卡本身就从
# DOM 里消失，计数归零；保存之后换成确认视图同样没有它。2026-09-17 的实机测试
# 就栽在这——第一条提交成功、面板没关掉，而下一条以为没有面板开着，去点被盖住的
# 行按钮，30 秒超时，整站判失败。
#
# 可见的「关闭菜单」按钮在**三个视图里都在**（卡片 / 钻入 / 保存后确认），实测过。
_PANEL_OPEN = """
(contract) => {
  const vis = el => el && el.offsetParent !== null;
  // 卡片视图：决议卡可见（实测 关 0 / 开 4）。
  if ([...document.querySelectorAll(contract.resolutionCard)].filter(vis).length) return true;
  // 钻入视图与保存后的确认视图里决议卡会消失，但这段说明一直在（实测 关 false /
  // 开 true）。两者取并集，任何一层侧栏都认得出来。
  return /为什么您需要提供警告和安全信息/.test(document.body.innerText || "");
}
"""

# 保存后那颗「关闭」按钮的位置。按坐标点，理由和复选框一样：
# ``locator(...).first`` 不按可见性过滤，会选到被挡住的同名元素然后超时。
_CLOSE_RECT = """
(contract) => {
  const vis = el => el && el.offsetParent !== null;
  const label = contract.closeLabel;
  const buttons = [...document.querySelectorAll("kat-button")].filter(
    el => vis(el) && (el.getAttribute("label") || "").trim() === label);
  if (buttons.length !== 1) return null;
  const r = buttons[0].getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
}
"""

# 可见复选框的位置。生产代码按这个坐标做**真实鼠标点击**——宿主元素上的 JS
# 合成点击传不进 shadow DOM，而且失败是静默的。
_CHECKBOX_RECT = """
(contract) => {
  const vis = el => el && el.offsetParent !== null;
  const box = [...document.querySelectorAll(contract.checkbox)].filter(vis)[0];
  if (!box) return null;
  const r = box.getBoundingClientRect();
  return {x: r.left, y: r.top, w: r.width, h: r.height};
}
"""

# ⚠️ 这一段是整条流程里唯一不可逆的动作。
# 所有不变量都在**同一个 JS tick 内**复核，然后才点——中间不回 Python，
# 页面没有机会在「检查通过」和「真的点下去」之间换掉内容。
_SAVE = """
(payload) => {
  const contract = payload.contract;
  %s
  const boxes = [...document.querySelectorAll(contract.checkbox)].filter(vis);
  if (boxes.length !== 1) return {ok: false, reason: "checkbox_not_unique", count: boxes.length};
  const box = boxes[0];
  if (box.getAttribute("value") !== "true") return {ok: false, reason: "checkbox_not_ticked"};

  const panel = panelFrom(box);
  if (!panel) return {ok: false, reason: "panel_without_asin"};
  const asins = asinsIn(panel);
  if (asins.length !== 1) return {ok: false, reason: "panel_asin_ambiguous", asins};
  if (asins[0] !== payload.asin) {
    return {ok: false, reason: "panel_asin_mismatch", found: asins[0], wanted: payload.asin};
  }

  const saves = [...document.querySelectorAll(contract.saveButton)].filter(vis);
  if (saves.length !== 1) return {ok: false, reason: "save_not_unique", count: saves.length};
  const save = saves[0];
  if (save.hasAttribute("disabled")) return {ok: false, reason: "save_disabled"};

  save.click();
  return {ok: true, asin: asins[0]};
}
""" % _PANEL_HELPERS


@dataclass(frozen=True, slots=True)
class PanelState:
    """打开的安全证明面板当前的样子。"""

    ok: bool
    asin: str = ""
    status: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ListOutcome:
    """这个站点的合规列表能不能读，以及为什么不能。"""

    ok: bool
    rows_loaded: bool = False
    reason: str = ""
    landed_url: str = ""
    # 切换页选站失败时的具体原因（``SwitchOutcome.reason``），进事件 details。
    switch_reason: str = ""
    # ⚠️ 登录助手可能把登录收在**另一个标签**里（亚马逊会这么干，并故意把原标签
    # 留在 ``/ap/signin``）。抓取必须在这个 Page 上做，否则会去读那个被遗弃的
    # 登录页——那上面一行都匹配不到，结果就是「这家店没有待办」。
    page: Any = None


class AmazonGpsrPage:
    def __init__(
        self,
        contract: GpsrDomContract | None = None,
        *,
        settle_seconds: float = 3.0,
        list_timeout_seconds: float = 45.0,
        panel_timeout_seconds: float = 20.0,
        login_advancer: AmazonLoginAdvancer | None = None,
        account_switcher: AmazonAccountSwitcher | None = None,
    ) -> None:
        self.contract = contract or GpsrDomContract()
        self.settle_seconds = settle_seconds
        self.list_timeout_seconds = list_timeout_seconds
        self.panel_timeout_seconds = panel_timeout_seconds
        # 登录、选站、落地判据:三条流程共用一份(见 amazon_landing 模块头部)。
        # 登录助手等紫鸟填好之后依次尝试邮箱 Continue、紫鸟托管 Passkey、已填密码
        # 登录、已填好的 6 位 OTP,以及两步验证方式选择页。凭据全程不经过这里。
        self._switcher = account_switcher or AmazonAccountSwitcher()
        self._landing = AmazonLanding(
            settle_seconds=settle_seconds,
            login_advancer=login_advancer,
            account_switcher=self._switcher,
        )

    @property
    def _payload(self) -> dict[str, str]:
        return self.contract.as_dict()

    async def open_policies(self, page: Any, domain: str) -> ListOutcome:
        """打开监管合规性页并等它的表格出现。

        **五路区分，一个都不能省**（2026-09-16 与 09-18 两次实地踩到过）：

        * ``auth_required`` —— 被弹到 ``/ap/*``。德国站有自己的
          ``assoc_handle``，会话掉得很快；实测同一个会话一小时前还能读到 51 行，
          再跑就落在登录页上。
        * ``marketplace_not_selected`` —— 落到账户/站点切换页，**而且自己选不掉**
          （列表里没有整串精确匹配的那个国家、或匹配到不止一个、或按钮被挡住、
          或点了没落地）。能选掉时这里根本不会返回——见下面那段。
          ⚠️ 落到切换页**不等于**「没在这个站点开卖」：2026-09-18 正香-张刚CA
          实测，切换页上「德國」在列且不是「待註冊」，账号是有这个站点的。
        * ``marketplace_unavailable`` —— 落到**其它**非预期路径。
        * ``ok`` + ``rows_loaded`` —— 页面对、表格也渲染出来了。
        * ``ok`` 但没有 ``rows_loaded`` —— 页面对但一行都没出现。

        只有「页面对、但表格结构始终没出现」算契约错误。

        ⚠️ 为什么这道判据是承重的：登录页上 ``div.ahd-product-policy-table-row``
        匹配到 **0 个**，于是纯筛选函数会报「0 候选、0 漂移、无异常」——和「这家
        店真的没有待办」一模一样。落地判据必须排在筛选之前。
        """

        landing = await self._landing.land_on(
            page,
            url=policies_url(domain),
            expected_path=PRODUCT_POLICIES_PATH,
            domain=domain,
        )
        page = landing.page or page
        if not landing.ok:
            return ListOutcome(
                ok=False,
                reason=landing.reason,
                landed_url=landing.landed_url,
                switch_reason=landing.switch_reason,
                page=page,
            )

        try:
            # ⚠️ 等**表格骨架**，不是等「行」。一家没有合规问题的店铺页面完全正常、
            # 表头也在，就是一行都没有（2026-09-17 秦登美实测：筛选器
            # 「监管合规性 (0)」、骨架在、rows=0）。等行等不到就抛契约错误，等于把
            # 最健康的状态判成故障——而德国站要铺到所有账号，这种店会越来越多。
            await page.wait_for_selector(
                self.contract.table_wrapper, timeout=45_000, state="attached"
            )
        except Exception as exc:  # noqa: BLE001 —— 作为契约错误响亮暴露
            raise DomContractError(
                f"监管合规性页未出现预期的表格结构（契约 {self.contract.version}）"
            ) from exc

        deadline = self.list_timeout_seconds
        waited = 0.0
        # ⚠️ 下限不能去掉：``settle_seconds=0`` 时 ``waited += step`` 永远加 0，
        # 这个循环就再也出不来了——服务会挂在一个「只读」的等待里，没有超时、
        # 没有日志、没有任何征兆。
        step = max(self.settle_seconds, 0.05)
        while waited < deadline:
            await page.wait_for_timeout(int(step * 1000))
            waited += step
            if await page.evaluate(_ROW_COUNT, self._payload):
                # 再稳一拍，让还在渲染的那批补齐。
                await page.wait_for_timeout(int(step * 1000))
                return ListOutcome(ok=True, rows_loaded=True, landed_url=landing.landed_url, page=page)

        # 一行都没读到。这时候必须分清「这家店真的干净」和「页面变了」——
        # 筛选器徽章是**亚马逊自己给出的权威条数**：它说有 N 条而我们一行都没读到，
        # 那就是结构漂移，绝不能报成「没有待办」。
        body = await page.evaluate("() => document.body.innerText || ''")
        chip = FILTER_CHIP_PATTERN.search(body or "")
        if chip and int(chip.group(1)) > 0:
            raise DomContractError(
                f"监管合规性页显示有 {chip.group(1)} 条记录，却一行都没读到"
                f"（契约 {self.contract.version}）"
            )
        return ListOutcome(ok=True, rows_loaded=False, landed_url=landing.landed_url, page=page)

    async def read_rows(self, page: Any) -> list[dict[str, Any]]:
        """把每一行抓成一个笨拙的字典。**不做任何判断。**

        返回值直接喂给 ``config.select_actionable``。这里多写一个 if，就等于把
        它放进一段永远测不到的代码里。
        """

        scraped = await page.evaluate(_SCRAPE_ROWS, self._payload)
        return list(scraped or ())

    # ------------------------------------------------------------------
    # 会动手的那半边
    # ------------------------------------------------------------------
    async def open_attestation_panel(self, page: Any, asin: str) -> PanelState:
        """点开某一行的「提交」，并确认打开的面板确实属于这个 ASIN。

        两道独立的闸：

        1. **从 0 到 1**。点之前页面上必须一张「安全证明」卡都没有，点完必须恰好
           一张。这条判据盯的是**结果状态**，而不是「我点了按钮」。
        2. **面板自报的 ASIN 必须等于目标**。实测面板头部会写出自己的 ASIN，且
           只有一个（2026-09-17）——这是防止「点开 A 的面板、把声明提交到 B 头上」
           的真实依据，不是靠点击顺序推断的。

        任何一条不成立都返回 ``ok=False``，由调用方决定怎么办。
        """

        before = await page.evaluate(_PANEL_OPEN, self._payload)
        if before:
            return PanelState(ok=False, reason="panel_already_open")

        await page.click(f"#{ACTION_BUTTON_PREFIX}{asin}", timeout=25_000)

        deadline = self.panel_timeout_seconds
        waited = 0.0
        step = max(self.settle_seconds, 0.05)
        state: dict[str, Any] = {}
        while waited < deadline:
            await page.wait_for_timeout(int(step * 1000))
            waited += step
            state = await page.evaluate(_PANEL_STATE, self._payload)
            # ⚠️ 状态**也要**等出来，不能只等面板出现。侧栏的骨架先渲染、
            # ``kat-label[text]`` 后到；只等前者会读到一个空状态，然后按
            # 「状态不认识」拒绝提交——看起来像判据出错，其实是退出轮询太早。
            if state.get("ok") and str(state.get("status") or "").strip():
                break
        if not state.get("ok"):
            return PanelState(ok=False, reason=str(state.get("reason") or "panel_never_opened"))

        found = str(state.get("asin") or "")
        if found != asin:
            # 面板开出来了，但它不是我们要的那个商品。绝不继续。
            return PanelState(ok=False, asin=found, reason="panel_asin_mismatch")
        return PanelState(ok=True, asin=found, status=str(state.get("status") or ""))

    async def tick_attestation(self, page: Any) -> bool:
        """进入「安全证明」卡并勾上那个唯一的复选框，然后**回读确认**。

        回读不能省：``kat-checkbox`` 的状态在 ``value`` 属性上而不是 ``checked``，
        点了没生效是静默的。
        """

        await page.get_by_text("安全证明", exact=True).last.click(timeout=25_000)
        await page.wait_for_timeout(int(max(self.settle_seconds, 0.05) * 1000))

        # ⚠️ 三种点法实测过，只有一种有效（2026-09-17）：
        #   - JS 的 ``host.click()``：**无效**。真正的 input 在 shadow DOM 里，
        #     宿主元素上的合成点击根本传不下去，而且**静默**——value 一直是 false。
        #   - ``page.locator("kat-checkbox").first.click()``：**点错元素**。这个
        #     定位器不按可见性过滤，会解析到页面上被挡住的筛选器复选框
        #     「所有政策类型 (85)」，然后在可操作性检查上超时。
        #   - **按可见那一个的坐标做真实鼠标点击**：有效。
        rect = await page.evaluate(_CHECKBOX_RECT, self._payload)
        if not rect:
            return False
        # 往左靠：方框本身在最左端，点那里最稳。
        await page.mouse.click(
            rect["x"] + min(12.0, float(rect["w"]) / 2),
            rect["y"] + float(rect["h"]) / 2,
        )
        await page.wait_for_timeout(int(max(self.settle_seconds, 0.05) * 1000))
        confirmed = await page.evaluate(
            "(c)=>{const b=[...document.querySelectorAll(c.checkbox)]"
            ".filter(e=>e.offsetParent!==null);"
            "return b.length===1 && b[0].getAttribute('value')==='true';}",
            self._payload,
        )
        return bool(confirmed)

    async def save_attestation(self, page: Any, asin: str) -> SaveOutcome:
        """⚠️ 整条流程唯一不可逆的动作。

        每一条不变量都在**同一个 JS tick 内**复核，然后才点保存——中间不回
        Python，页面没有机会在「检查通过」和「真的点下去」之间换掉内容。

        复核的四件事：唯一的复选框且已勾上、面板自报的 ASIN 仍等于目标、
        唯一的保存按钮、它没有 disabled。
        """

        result = await page.evaluate(
            _SAVE, {"contract": self._payload, "asin": asin}
        )
        if not result.get("ok"):
            return SaveOutcome(
                ok=False,
                reason=str(result.get("reason") or "save_refused"),
                detail=str(result.get("found") or result.get("count") or ""),
            )
        await page.wait_for_timeout(int(max(self.settle_seconds, 0.05) * 1000))
        return SaveOutcome(ok=True)

    async def capture_evidence(
        self, page: Any, run: Any, marketplace_code: str, *, label: str
    ) -> Any | None:
        """在点保存的前一刻拍一张现场。

        运营 2026-09-16 决定这条流程要留证：勾那个框是向亚马逊做**不可逆的合规
        声明**，而系统本来不会留下任何「当时页面长什么样」的凭据。文件按现有的
        90 天 ``artifact_retention_days`` 自动清理。
        """

        import hashlib
        import re as _re
        from pathlib import Path

        from ..types import EvidenceRef

        if getattr(run, "artifact_dir", None) is None:
            return None
        directory = Path(run.artifact_dir) / run.id
        directory.mkdir(parents=True, exist_ok=True)
        safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "evidence"
        target = directory / f"{marketplace_code}-{safe}.png"
        await page.screenshot(path=str(target), full_page=True)
        return EvidenceRef(
            kind="screenshot",
            file_path=str(target),
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            metadata={"marketplace": marketplace_code, "label": label},
        )

    async def return_to_list(self, page: Any, domain: str) -> bool:
        """把页面收回安全态：**先像人一样点「关闭」**，不行才重新加载。

        顺序是运营 2026-09-17 提的，理由充分：每处理一条就整页刷新一次，连续九次，
        自动化特征比点一下就在眼前的关闭按钮明显得多。而页面本来就给了那颗按钮。

        两条路都有**结果判据**（``_PANEL_OPEN``），点完不算数、刷完也不算数，
        要判据说侧栏没了才算。所以「点关闭」这条路即使选错了元素也不会造成损失，
        最坏是退回重新加载——那条路是实测过的。

        ⚠️ 为什么不用别的关闭方式（都实测排除过）：
        - Esc **关不掉**这个侧栏。
        - ``button[aria-label="关闭菜单"]`` 是**页面自己的导航开关**，坐标 x=-48 在
          屏幕外，侧栏开不开它都在——拿它当判据或点击目标都是错的。
        - 侧栏头部那颗 ✕ 在页面滚到下面时 ``y`` 是**负数**（实测 -706），点不到。

        ⚠️ **安全态不等于「没有侧栏」，而是「回到了合规列表页」。** 只问 ``_PANEL_OPEN``
        会把「被弹回登录页」也判成安全：登录页上没有决议卡、正文也不匹配，判据返回
        false，于是循环继续，下一条去点一个根本不存在的行按钮、等满 25 秒超时，
        站点被判 FAILED，而报出来的原因和真实原因（会话掉了）毫无关系。
        德国站会话掉得很快，而一次 AUTO 要连做十几条、每条都重新导航一次。

        :returns: 是否确认回到安全态。
        """

        if await self._is_safe_state(page):
            return True

        # ① 点「关闭」。按坐标点的理由和复选框一样：locator 不按可见性过滤。
        rect = await page.evaluate(_CLOSE_RECT, self._payload)
        if rect:
            try:
                await page.mouse.click(rect["x"], rect["y"])
                await page.wait_for_timeout(
                    int(max(self.settle_seconds, 0.05) * 1000)
                )
            except Exception:  # noqa: BLE001 —— 下面的判据才是裁判
                logger.info("gpsr: 点击关闭按钮失败，改用重新加载")
            if await self._is_safe_state(page):
                return True
            logger.info("gpsr: 点了关闭仍未回到安全态，改用重新加载")

        # ② 退回重新加载。新文档无条件关掉一切，顺带刷新列表。
        try:
            await page.goto(
                policies_url(domain), wait_until="domcontentloaded", timeout=90_000
            )
            await page.wait_for_timeout(int(max(self.settle_seconds, 0.05) * 1000))
        except Exception:  # noqa: BLE001
            logger.info("gpsr: 重新加载列表页失败")

        safe = await self._is_safe_state(page)
        if not safe:
            logger.warning("gpsr: 无法收回安全态，后续条目无法处理")
        return safe

    def unsafe_reason(self, page: Any) -> str:
        """收不回安全态时,**看见的是什么**。

        德国站会话掉得很快,重新加载列表页经常直接落到登录页。一律报「侧栏未能
        关闭」的话,操作员拿到的原因和真实原因毫无关系(2026-09-26 自审;
        ``return_to_list`` 的 docstring 早就点名了这种错位)。
        """

        path = urlparse(str(getattr(page, "url", "") or "")).path.rstrip("/")
        if path.startswith("/ap/") or path == "/ap":
            return "auth_required"
        if self._switcher.is_switcher_url(str(getattr(page, "url", "") or "")):
            return "marketplace_not_selected"
        if path != PRODUCT_POLICIES_PATH.rstrip("/"):
            return "left_the_page"
        return "panel_stuck"

    async def _is_safe_state(self, page: Any) -> bool:
        """可以继续处理下一条的状态：**在合规列表页上，且侧栏已关闭**。

        两个条件缺一不可——只问侧栏会把登录页判成安全（见 ``return_to_list``），
        只问路径会把「还在合规页但侧栏盖着行按钮」判成安全（那是 2026-09-17 实机
        测试里第一条提交成功之后整站失败的原因）。
        """

        path = urlparse(str(getattr(page, "url", "") or "")).path.rstrip("/")
        if path != PRODUCT_POLICIES_PATH.rstrip("/"):
            return False
        return not await page.evaluate(_PANEL_OPEN, self._payload)


__all__ = ["AmazonGpsrPage", "ListOutcome", "PanelState", "SaveOutcome"]
