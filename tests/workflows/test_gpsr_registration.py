"""GPSR 流程的注册。

这里钉两件事：接进去之后**服务还能起来**，以及**模式的默认值是安全的那个**。

AUTO 曾经刻意**不在选项里**——勾那个复选框是向亚马逊做不可逆的合规声明，而运营
已经拍板不设逐条人工闸门，所以在真机验证之前它不该只是「没人选」。2026-09-17
空跑核对与首次单条提交都确认无误后才放开；默认值仍然是 DRY_RUN。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ziniao_automation.workflows.amazon_disbursement import (
    build_amazon_disbursement_definition,
)
from ziniao_automation.workflows.amazon_feedback import (
    build_amazon_feedback_definition,
)
from ziniao_automation.workflows.amazon_gpsr import (
    AmazonGpsrConfig,
    AmazonGpsrWorkflow,
    build_amazon_gpsr_definition,
)
from ziniao_automation.workflows.dispatcher import WorkflowDispatcher
from ziniao_automation.workflows.registry import (
    WorkflowExecutionClass,
    WorkflowRegistry,
)
from ziniao_automation.workflows.types import RunMode


def _definition():
    return build_amazon_gpsr_definition(AmazonGpsrWorkflow(repository=object()))


def _named(key: str):
    """注册表强制 ``definition.key == workflow.name``，替身也得有名字。"""

    return type("FakeWorkflow", (), {"name": key})()


def _all_three():
    return WorkflowRegistry(
        (
            build_amazon_disbursement_definition(_named("amazon_disbursement")),
            build_amazon_feedback_definition(_named("amazon_feedback_removal")),
            _definition(),
        )
    )


def test_the_dispatcher_accepts_all_three_workflows() -> None:
    """``WorkflowDispatcher`` 在**构造期**校验：每条定义都要绑定执行器，
    且它的 execution_class 都要有引擎。少一样服务就起不来——所以这条断言
    等价于「加了这条流程之后后台还能启动」。
    """

    engine = object()
    registry = _all_three()
    dispatcher = WorkflowDispatcher(
        registry=registry,
        engines={
            WorkflowExecutionClass.FINANCIAL.value: engine,
            WorkflowExecutionClass.STANDARD.value: engine,
        },
        repository=object(),
    )
    assert dispatcher.registry is registry


def test_an_unbound_definition_is_refused() -> None:
    """反向验证上面那条断言不是空转：没绑执行器就必须炸。"""

    registry = WorkflowRegistry(
        (
            build_amazon_disbursement_definition(_named("amazon_disbursement")),
            build_amazon_gpsr_definition(),  # 没有 workflow
        )
    )
    with pytest.raises(ValueError, match="missing executable bindings"):
        WorkflowDispatcher(
            registry=registry,
            engines={
                WorkflowExecutionClass.FINANCIAL.value: object(),
                WorkflowExecutionClass.STANDARD.value: object(),
            },
            repository=object(),
        )


def test_auto_is_available_but_never_the_default() -> None:
    """AUTO 于 2026-09-17 放开（首次单条真实提交已确认落地）。

    但**默认仍是 DRY_RUN**：建排期时手滑选不到自动提交。这条流程的每一次提交
    都是不可撤销的合规声明，默认值必须是只看不做的那个。

    APPROVAL 仍然不列——它的人工闸门是资金审核台，整个页面围绕金额和收款账户建，
    一条 GPSR 任务停在那里会渲染成 EUR 0.00 的提现。
    """

    definition = _definition()
    assert set(definition.supported_modes) == {RunMode.DRY_RUN, RunMode.AUTO}
    assert definition.default_mode is RunMode.DRY_RUN
    assert RunMode.APPROVAL not in definition.supported_modes


def test_there_is_no_per_run_submission_cap() -> None:
    """⚠️ 运营明确要求不设上限，所以一次 AUTO 会把**所有**候选做完。

    这条断言不是在庆祝这个决定，是在标记它：反馈流程有
    ``max_submissions_per_run``，这条**刻意没有**。剩下的爆炸半径控制只有
    workflow 里那只金丝雀（第一条没确认落地就停整站）。谁要是哪天想加回上限，
    会先看到这里。
    """

    fields = _definition().config_model.model_fields
    assert "max_submissions_per_run" not in fields
    assert set(fields) == {"marketplace_codes"}


def test_it_never_takes_the_global_funds_lock() -> None:
    """一条 GPSR 扫描可能跑很久；占全局资金锁会拖垮所有店铺的提现，
    而提现按滑动 24 小时限流，拖过了就是漏一笔。
    """

    definition = _definition()
    assert definition.requires_financial_lock is False
    assert definition.execution_class is WorkflowExecutionClass.STANDARD
    # 队列争用时提现永远优先。
    assert definition.business_priority > build_amazon_disbursement_definition().business_priority


def test_the_definition_key_matches_the_executor_name() -> None:
    """注册表强制两者一致；对不上时的报错信息很难懂，这里直接钉住。"""

    assert _definition().key == AmazonGpsrWorkflow.name == "amazon_gpsr_attestation"


# --------------------------------------------------------------------------
# 配置模型
# --------------------------------------------------------------------------
def test_only_the_german_marketplace_is_selectable() -> None:
    """GPSR 是欧盟法规，我们只有德国站在欧盟。

    写死成 DE 是 fail-closed 的：指向 CA/UK/AU 只会打开一个结构完全不同的页面，
    与其到时候靠页面判据兜，不如在排期表单上就选不出来。
    """

    assert AmazonGpsrConfig(marketplace_codes=("DE",)).marketplace_codes == ("DE",)
    for rejected in ("CA", "UK", "AU", "US"):
        with pytest.raises(ValidationError):
            AmazonGpsrConfig(marketplace_codes=(rejected,))


def test_marketplace_codes_are_normalised_and_deduplicated() -> None:
    config = AmazonGpsrConfig(marketplace_codes=(" de ", "DE"))
    assert config.marketplace_codes == ("DE",)


def test_a_bare_string_is_not_mistaken_for_a_list_of_codes() -> None:
    """``"DE"`` 会被迭代成 ``('D', 'E')``，两个都不是合法站点——但错误信息会
    变得莫名其妙。这里在入口就挡掉。
    """

    with pytest.raises(ValidationError):
        AmazonGpsrConfig(marketplace_codes="DE")


def test_unknown_config_fields_are_rejected() -> None:
    """``extra="forbid"``：排期里写了一个不存在的字段必须报错，不能被默默忽略。

    尤其是 ``max_submissions_per_run``——反馈流程有这个字段，本流程**刻意没有**
    （运营要求不设上限）。如果它被静默吞掉，操作员会以为自己设了限。
    """

    with pytest.raises(ValidationError):
        AmazonGpsrConfig(marketplace_codes=("DE",), max_submissions_per_run=5)


def test_the_config_is_frozen() -> None:
    config = AmazonGpsrConfig(marketplace_codes=("DE",))
    with pytest.raises(ValidationError):
        config.marketplace_codes = ("DE", "DE")


# --------------------------------------------------------------------------
def test_the_public_schema_exposes_de_as_the_only_choice() -> None:
    """排期页的站点勾选框是按这个 schema 渲染的。"""

    schema = _definition().to_public_dict()["config_schema"]
    field = schema["properties"]["marketplace_codes"]
    rendered = field["items"]
    # pydantic 对单值 Literal 生成 const 而不是 enum；两种写法都要能取到 DE。
    values = rendered.get("enum") or [rendered["const"]]
    assert values == ["DE"]
    assert schema.get("additionalProperties") is False
