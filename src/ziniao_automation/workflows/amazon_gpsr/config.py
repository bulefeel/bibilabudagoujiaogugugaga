"""德国站「监管合规性」页的 DOM 契约，以及挑出可提交条目的纯函数。

这个模块里没有 I/O。页面那层只负责把每一行抓成一个笨拙的字典（因为测试夹具
``tests/workflows/fixture_dom.py`` 只有 ``Locator.evaluate``、没有页面级
``evaluate``，而 Katal 的 shadow DOM 必须走页面级 ``evaluate`` 才读得到——所以
凡是放在 JS 里的判断都测不到）。**所有筛选规则都必须留在这里**，这是它们唯一
能被覆盖的地方。

判据全部来自 2026-09-12/14 在秦登友德国站实地读到的 DOM，不是推断：

    行        div.ahd-product-policy-table-row
    原因格    第 0 格，带 .ahd-product-policy-table-row-reason
    动作按钮  kat-button#ahd-action-button-<ASIN>   label="提交"
    列顺序    原因 / 日期 / 商品 / 存在销售风险 / 采取的操作 / 账户状况评级影响 / 后续步骤

四个实测出来的坑，四条规则一一对应：

1. **同一个 ASIN 在 DOM 里出现 3 次**（响应式多套渲染同时存在）。实测 9 个
   「警告和安全信息」行节点其实只是 3 个 ASIN。不按 ASIN 去重就会对同一条做
   三次不可逆提交。
2. **只有第 0 格有语义 class**，其余全是 ``kat-col-xs-N``，**靠位置**。所以
   亚马逊插一列就会让「采取的操作」错位——于是用两道保险：单元格数必须是 7，
   且「商品已移除」在**任意一格**出现都排除。
3. **CSS class 全是构建期哈希**（``SdLjr+k5iAXDN4y-JpkwbA==``、
   ``data-testid="resolution-path-card-<uuid>"``），每次亚马逊发版都会变，
   一个都不能当选择器用。能用的只有 aria-label、ASIN 键控的 id、kat-* 标签名
   和可见文本。
4. **折叠组的组标题按钮 id 是数字 case id，而且三个分组共用同一个**
   （``ahd-action-button-21862491`` 在 负责人/制造商/EPR 三处都出现）。它有歧义，
   永远不能点。ASIN 形状校验顺带把它挡在外面。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Sequence


PRODUCT_POLICIES_PATH = "/performance/account/health/product-policies"
# 这个查询串会**自动带上「监管合规性」筛选**，实测过，不需要从账户状况点进来。
REGULATORY_TAB_QUERY = "?t=regulatory-compliance"



def policies_url(domain: str) -> str:
    return f"https://{domain}{PRODUCT_POLICIES_PATH}{REGULATORY_TAB_QUERY}"


# ---------------------------------------------------------------------------
# 原因文案。注意标点在亚马逊自己的 DOM 里就不一致，别"顺手统一"：
#   GPSR：警告和安全信息      全角冒号，**无**空格
#   GPSR： 负责人详细联系信息  全角冒号，**有**空格
#   GPSR：制造商详细联系信息  全角冒号，无空格
#   EPR: 德国 WEEE-小型设备    **半角**冒号 + 空格
# ---------------------------------------------------------------------------
TARGET_REASON = "GPSR：警告和安全信息"

KNOWN_REASONS: frozenset[str] = frozenset(
    {
        TARGET_REASON,
        "GPSR： 负责人详细联系信息",
        "GPSR：制造商详细联系信息",
        "EPR: 德国 WEEE-小型设备",
        "欧洲增值税登记要求",
    }
)

# 采取的操作列读到这个，说明商品已经被下架了。运营明确要求这类不提交——对一个
# 已经下架的商品做「本商品不需要警告和安全信息」的不可逆声明毫无意义。
#
# ⚠️ 比对前**两边都要去掉全部空白**。这个短语里一个空白都没有，而 innerText 很
# 容易在里面插入换行（比如亚马逊把状态值包进 kat-badge 这类块级自定义元素）。
# 注意不能用 `_normalize`：它是把空白**压成一个空格**而不是删掉，对「商品 已移除」
# 照样匹配不上——按那个方子改会得到一个看着修好、实际没变的补丁。
ACTION_ALREADY_REMOVED = "商品已移除"

# 反过来，可提交的行必须**正面**认出「有移除风险」。黑名单是 fail-open 的：
# 亚马逊改一次文案，被排除的行就会变成候选；白名单是 fail-closed 的，改了文案
# 只会让候选变 0，而那会被下面的兜底闸捅出来。
ACTION_AT_RISK = "商品有移除风险"

# 侧栏「安全证明」卡片右上角的状态。这是**幂等的载体**：状态写在亚马逊页面上，
# 所以这条流程不需要自己的台账表。
STATUS_ACTIONABLE = "未开始"
STATUS_SUBMITTED = "审核中"

EXPECTED_CELL_COUNT = 7
ACTION_CELL_INDEX = 4

# ASIN 是定长 10 位大写字母数字。这条形状校验同时挡掉了组标题的数字 case id
# （21862491，8 位）和负责人那类组内行的 SKU（JN-GDNQGMX-1GZ，含连字符）。
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")

ACTION_BUTTON_PREFIX = "ahd-action-button-"

# 筛选器徽章：「监管合规性 (14)」。这是**亚马逊自己给出的权威条数**，用来和我们
# 读到的行数交叉核对——它说有 14 条而我们一行都没读到，那就是结构漂移，不是
# 「这家店很干净」。两者都为 0 才是真的干净。
FILTER_CHIP_PATTERN = re.compile(r"监管合规性\s*[（(]\s*(\d+)\s*[)）]")


@dataclass(frozen=True, slots=True)
class GpsrDomContract:
    """指向德国站合规页的全部外部坐标，集中一处。"""

    version: str = "product-policies-v1"
    rows: str = "div.ahd-product-policy-table-row"
    reason_cell: str = ".ahd-product-policy-table-row-reason"
    accordion_header: str = "div.ahd-accordion__button"
    accordion_body: str = "div.ahd-accordion__body"
    action_button: str = "kat-button[id^='ahd-action-button-']"
    # 侧栏里「安全证明」那张卡。aria-label 是唯一稳定的锚点——卡片的
    # data-testid 是随机 uuid，class 是构建期哈希。
    attestation_card: str = 'div[role="button"][aria-label="安全证明"]'
    card_status: str = "kat-label"
    checkbox: str = "kat-checkbox"
    # 2026-09-17 实地读到的稳定锚点。保存按钮的 data-testid 是少数几个不是
    # 构建期哈希的属性之一；勾选前它带 disabled。
    save_button: str = 'kat-button[data-testid="primary-button"]'
    back_button: str = 'kat-button[data-testid="back-button"]'
    # 侧栏是否开着，只看它自己的决议卡。实测：关着 0 个、开着 4 个。
    #
    # ⚠️ 不能用 `button[aria-label="关闭菜单"]` —— 那是页面自己的导航开关，它的
    # 坐标是 x=-48（屏幕外），面板开不开它都在。也不能数「安全证明」卡：一钻进
    # 那张卡它就从 DOM 里消失。
    resolution_card: str = "[data-testid^='resolution-path-card-']"
    # 保存成功后那个确认视图底部的整宽按钮。⚠️ 这个文案来自用户 2026-09-17 的
    # 截图，是**唯一还没被我实地读过 DOM 的判据**——写这段时德国站已经没有
    # 「未开始」的条目可以复现那个视图了。所以点它之后必须验证，验证不过就退回
    # 重新加载（那条路是实测过的）。第一次真机遇到时把结果记回来。
    close_label: str = "关闭"
    # 表格骨架。⚠️ 结构判据必须等**它**，不能等「行」：一家没有合规问题的店铺
    # 页面完全正常、表头也在，就是一行都没有（2026-09-17 秦登美实测：
    # 筛选器写着「监管合规性 (0)」、tableWrapper=2、rows=0）。等行等不到就抛契约
    # 错误，等于把最健康的状态判成故障。
    table_wrapper: str = "[class*='ahd-product-policy-table']"
    save_label: str = "保存"
    back_label: str = "返回"

    def as_dict(self) -> dict[str, str]:
        """给浏览器用的纯字典（JS 那边收不到 dataclass）。"""

        return {
            "rows": self.rows,
            "reasonCell": self.reason_cell,
            "accordionHeader": self.accordion_header,
            "accordionBody": self.accordion_body,
            "actionButton": self.action_button,
            "attestationCard": self.attestation_card,
            "cardStatus": self.card_status,
            "checkbox": self.checkbox,
            "saveButton": self.save_button,
            "resolutionCard": self.resolution_card,
            "tableWrapper": self.table_wrapper,
            "closeLabel": self.close_label,
        }


class GpsrContractError(RuntimeError):
    """页面结构和契约对不上。响亮失败，绝不降级成「没有待办」。"""


@dataclass(frozen=True, slots=True)
class PolicyRow:
    """一个**去重后**的可提交条目。"""

    asin: str
    reason: str
    title: str
    action_taken: str
    status_text: str
    dom_copies: int


@dataclass(frozen=True, slots=True)
class Selection:
    """挑选结果。计数字段用来写 ``gpsr_scanned`` 事件。

    「0 个候选」必须能和「页面变了所以一个都没认出来」区分开，否则每家店会永远
    报绿而没人发现——这正是 ``reasons_seen`` 存在的理由。
    """

    candidates: tuple[PolicyRow, ...]
    rows_read: int
    reasons_seen: frozenset[str]
    already_removed: int
    drift_rows: int
    # 读到多少个**目标原因**的 ASIN（去重后，含已下架的）。用来判断「0 个候选」
    # 到底是这家店真的干净，还是目标行全被闸挡下了。
    target_asins: int
    # 白名单之外的原因文案。**不阻断**，但必须一路报到运行事件和飞书卡片上：
    # 它同时是「亚马逊新增了品类」和「亚马逊改了目标那句文案」的唯一信号，而后者
    # 会让候选静默变 0、每家店永远报绿。
    unknown_reasons: frozenset[str]
    # 因「商品已移除」而跳过的 ASIN。只给一个计数的话，操作员没法核对我们跳过的
    # 到底是不是他在页面上看到的那一条——而空跑预览是这条不可逆流程唯一的人工闸门。
    removed_asins: frozenset[str]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _normalize(value: Any) -> str:
    """折叠所有空白。亚马逊在单元格里混用 \\n 和不换行空格。"""

    return re.sub(r"\s+", " ", _text(value)).strip()


def _squeeze(value: Any) -> str:
    """删掉全部空白。用于比对内部没有空白的固定短语。

    和 ``_normalize`` 的区别是承重的：后者把空白压成**一个空格**，所以
    ``"商品 已移除"`` 经过它仍然是 ``"商品 已移除"``，照样匹配不上
    ``"商品已移除"``。判据用错函数等于没加。
    """

    return re.sub(r"\s+", "", _text(value))


# 折叠组的组标题会在原因后面挂上「N 受影响的 ASIN / N 受影响的 SKU」。
#
# ⚠️ 光靠「取第一行」去不掉它：2026-09-16 真机实测，三份响应式副本里只有一份用
# 换行分隔，另外两份**一个分隔符都没有**，直接拼成「…制造商详细联系信息3 受影响的
# ASIN3 受影响的 SKU」。于是这些组标题被当成「白名单之外的原因」，每次正常运行都
# 报一次假警——而在运营去掉阻断阀门之后，这个提醒是目标文案漂移的**唯一**信号，
# 天天误报就等于把它废掉。
_AFFECTED_SUFFIX = re.compile(r"(?:\s*\d+\s*受影响的\s*(?:ASIN|SKU))+\s*$", re.IGNORECASE)


def _first_line(value: Any) -> str:
    """原因格里第一行**有内容**的文本，并去掉组标题的「N 受影响的…」后缀。

    ⚠️ 取「第一行有内容的」而不是「第 0 行」：单元格里出现一个前置 ``<br>``、
    或者该格没被渲染而 innerText 退化成带源码缩进的 textContent，都会让第 0 行
    是空白——于是目标行既不进 ``reasons_seen`` 也不计 drift，**静默消失**。
    """

    for line in _text(value).split("\n"):
        normalized = _normalize(line)
        if normalized:
            return _AFFECTED_SUFFIX.sub("", normalized).strip()
    return ""


def _asin_from_button(button_id: Any) -> str | None:
    raw = _text(button_id).strip()
    if not raw.startswith(ACTION_BUTTON_PREFIX):
        return None
    candidate = raw[len(ACTION_BUTTON_PREFIX) :]
    return candidate if ASIN_PATTERN.fullmatch(candidate) else None


def _title_of(cells: Sequence[str]) -> str:
    """商品格里是「标题 + ASIN：… + SKU: …」，只取标题那一行。"""

    if len(cells) <= 2:
        return ""
    for line in _text(cells[2]).split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith(("ASIN", "SKU")):
            return _normalize(stripped)
    return _normalize(cells[2])


def select_actionable(rows: Iterable[Mapping[str, Any]]) -> Selection:
    """从抓到的行描述里挑出该提交的条目。

    每条规则都对应一个实测到的坑（见模块头）。放宽任何一条之前先回去读那一段——
    它们不是防御性兜底，是显式的转移判据。

    分两趟走，顺序是承重的：

    **第一趟**只收集「关于这个商品的事实」——见过的原因、哪些 ASIN 被亚马逊下架
    了。它**跑在所有资格闸之前**，因为「已下架」是**商品**的属性而不是 DOM 节点
    的属性：同一个 ASIN 有 3 份响应式副本，只要任意一份说它下架了，整个 ASIN 就
    不许提交。若把这件事放在资格闸之后收集，那份带标记的副本可能因为列数漂移、
    是组标题、或按钮 id 不对而先被跳掉，于是另外两份干净的副本说了算——**最宽松
    的那份副本决定了一次不可逆的合规声明**。

    **第二趟**才判资格，最后用第一趟的事实做否决。

    :raises GpsrContractError: 明明读到了目标原因的条目，却一条都处理不了。那是
        「闸门通过、实际什么也没发生」，绝不能报成「没有待办」。
        （不认识的原因**不**抛异常，只记进 ``Selection.unknown_reasons``。）
    """

    prepared: list[tuple[list[str], str, str | None, Mapping[str, Any]]] = []
    reasons_seen: set[str] = set()
    # 按 ASIN 键控：已下架同样有 3 份副本，按行计数会把 1 个商品报成 3 个。
    removed: set[str] = set()
    target_asins: set[str] = set()
    rows_read = 0

    for row in rows:
        rows_read += 1
        cells = [_text(cell) for cell in (row.get("cells") or ())]
        reason = _first_line(cells[0] if cells else row.get("reason"))
        if reason:
            reasons_seen.add(reason)
        asin = _asin_from_button(row.get("buttonId"))
        # ⚠️ 不设任何前置条件。列数漂移、组标题、原因不是目标——通通不影响
        # 「亚马逊说这个商品已经下架了」这个事实的可信度。
        if asin is not None and any(
            ACTION_ALREADY_REMOVED in _squeeze(cell) for cell in cells
        ):
            removed.add(asin)
        # 组标题代表整组、不是一个条目，不计进「目标条目」——否则下面的兜底闸
        # 会因为一个本来就不该处理的东西而误报整站失败。
        if (
            asin is not None
            and reason == TARGET_REASON
            and not row.get("inAccordionHeader")
        ):
            target_asins.add(asin)
        prepared.append((cells, reason, asin, row))

    # 不认识的原因**只上报、不阻断**（运营 2026-09-16 明确要求）。亚马逊新增一个
    # 合规品类不该让整站停摆。
    #
    # ⚠️ 代价要知道：这道闸原本挡的不是「新增品类」，而是**亚马逊改了目标那一句
    # 文案**——那时候候选会静默变 0、任务报成功，每家店永远显示绿色而没人发现。
    # 现在这个信号靠 `unknown_reasons` 走运行事件和飞书卡片报出来，**没有任何
    # 代码会因此停下**，所以它只在有人真的去看的时候才有用。
    unknown_reasons = reasons_seen - KNOWN_REASONS

    collected: dict[str, PolicyRow] = {}
    copies: dict[str, int] = {}
    drift_rows = 0

    for cells, reason, asin, row in prepared:
        # 规则 5：列数漂移。7 列是实测值；对不上就不是我们认识的这张表，位置
        # 判据全部作废。单独计数，免得悄悄变成「没有待办」。
        if len(cells) != EXPECTED_CELL_COUNT:
            if reason == TARGET_REASON:
                drift_rows += 1
            continue

        # 规则 1：只处理目标原因。
        if reason != TARGET_REASON:
            continue

        # 规则 4：折叠组的组标题按钮 id 三个分组共用，有歧义，永不点击。
        if bool(row.get("inAccordionHeader")):
            continue

        # 规则 2+4：按钮 id 必须解析成形状正确的 ASIN。
        if asin is None:
            drift_rows += 1
            continue

        # 已下架：第一趟已经按 ASIN 认定过了。在白名单之前短路，否则它会被当成
        # 「结构漂移」计数——那个数字是给操作员看的，不能把正常跳过混进去。
        if asin in removed:
            continue

        # 规则 3 的正面判据：必须认得出「有移除风险」。白名单是 fail-closed 的，
        # 黑名单会在亚马逊改文案时把被排除的行变成候选。
        if ACTION_AT_RISK not in _squeeze(cells[ACTION_CELL_INDEX]):
            drift_rows += 1
            continue

        # 规则 6：按 ASIN 去重。同一条有 3 份响应式副本。
        copies[asin] = copies.get(asin, 0) + 1
        if asin in collected:
            continue
        collected[asin] = PolicyRow(
            asin=asin,
            reason=reason,
            title=_title_of(cells),
            action_taken=_normalize(cells[ACTION_CELL_INDEX]),
            status_text=_normalize(cells[EXPECTED_CELL_COUNT - 1]),
            dom_copies=0,
        )

    # 否决放在最后，才能同时覆盖「带标记的副本排在后面」的顺序。
    candidates = tuple(
        PolicyRow(
            asin=row.asin,
            reason=row.reason,
            title=row.title,
            action_taken=row.action_taken,
            status_text=row.status_text,
            dom_copies=copies.get(row.asin, 1),
        )
        for row in collected.values()
        if row.asin not in removed
    )

    # 兜底闸：一条候选都没产出，而又**存在至少一个无法用「已下架」解释的目标**
    # ——说明它们全被某道闸挡下了（列数变了、按钮 id 变了、采取的操作换了文案）。
    # 这是「闸门通过、实际什么也没发生」，必须响亮失败。
    #
    # ⚠️ 原来的豁免写成 ``not (target_asins & removed)``：**只要有任意一个已下架的
    # ASIN，整道闸就被整体豁免**。11 个目标里 1 个已下架、另外 10 个全被结构漂移
    # 挡掉时，闸不响、候选为空、站点报「没有待办」、run 绿卡——正是这段注释自己
    # 要防的那件事。豁免的范围只能是**那些真的已下架的**，不是全体。
    #
    # 部分漂移（还有候选跑得出来）照常干活，漂移条数由 ``drift_rows`` 报出来供
    # 人核对——那是刻意的，别顺手收紧成「有一行不认识就停整站」。
    if not candidates and (target_asins - removed):
        raise GpsrContractError(
            "德国站合规页读到了目标原因的条目，却一条都无法处理，已停止本站点；"
            f"目标 ASIN {len(target_asins)} 个、已下架 {len(target_asins & removed)} 个、"
            f"结构不符 {drift_rows} 行"
        )

    return Selection(
        candidates=candidates,
        rows_read=rows_read,
        reasons_seen=frozenset(reasons_seen),
        already_removed=len(target_asins & removed),
        drift_rows=drift_rows,
        target_asins=len(target_asins),
        unknown_reasons=frozenset(unknown_reasons),
        removed_asins=frozenset(target_asins & removed),
    )


__all__ = [
    "ACTION_ALREADY_REMOVED",
    "ASIN_PATTERN",
    "EXPECTED_CELL_COUNT",
    "GpsrContractError",
    "GpsrDomContract",
    "KNOWN_REASONS",
    "PolicyRow",
    "STATUS_ACTIONABLE",
    "STATUS_SUBMITTED",
    "Selection",
    "TARGET_REASON",
    "policies_url",
    "select_actionable",
]
