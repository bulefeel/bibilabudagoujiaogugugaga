# 固定工作流集成契约

当前代码白名单包含 `amazon_disbursement`、`amazon_feedback_removal` 和 `amazon_gpsr_attestation`，不加载网页上传脚本或动态模块。下面展示提现流程的注册示例。

```python
from ziniao_automation.workflows import (
    AutomationService,
    DatabaseRunLoader,
    SqlAlchemyWorkflowRepository,
    WorkflowEngine,
    WorkflowExecutionClass,
    WorkflowRegistry,
)
from ziniao_automation.workflows.dispatcher import WorkflowDispatcher
from ziniao_automation.workflows.amazon_disbursement import (
    AmazonDisbursementWorkflow,
    AmazonPaymentsPage,
    build_amazon_disbursement_definition,
)

repository = SqlAlchemyWorkflowRepository(session_factory)
workflow = AmazonDisbursementWorkflow(
    repository=repository,
    page_adapter=AmazonPaymentsPage(),
)
registry = WorkflowRegistry((build_amazon_disbursement_definition(workflow),))
engine = WorkflowEngine(
    registry=registry,
    repository=repository,
    browser_sessions=ziniao_controller,
    notifier=notifier,
)
dispatcher = WorkflowDispatcher(
    registry=registry,
    engines={WorkflowExecutionClass.FINANCIAL.value: engine},
    repository=repository,
)
loader = DatabaseRunLoader(
    session_factory,
    artifact_root=settings.evidence_dir,
    workflow_registry=registry,
)
automation = AutomationService(
    engine=dispatcher,
    run_loader=loader,
    recovery_loader=loader.recovery_runs,
    ziniao_controller=ziniao_controller,
    session_factory=session_factory,
)
```

排期与扩展约束：

- `GET /api/workflows` 只公开代码注册的固定展示元数据和配置字段定义。
- `POST /api/schedules/batch/preview` 先逐店检查；任意一家不合格时整批写入 0 条。
- `POST /api/schedules/batch` 使用 UUID `request_id` 保证重复点击只创建一次，并为每家店铺生成独立排期。
- 新流程必须新增 `WorkflowDefinition`、Pydantic 配置模型、执行器和页面适配器；网页不能上传脚本或提交未注册字段。
- 排期修改后，已经创建的 Run 与队列条目仍使用各自的配置、配置版本、业务优先级及批次顺序快照。

安全边界：

- `SqlAlchemyWorkflowRepository.arm_operation()` 在返回前提交 `ARMED`；之后才允许页面点击。
- 已处理站点根据资金记录防重复。`SUBMITTED` 表示已记录派发，提交后不再自动联网回读；`ARMED/UNCERTAIN` 未决记录仅按本地事实收口并交人工核对，不重放提交。
- 审核后重新读取域名、卖家 ID、付款账户、金额、结算周期及 DOM 指纹，按现有绑定哈希与金额检查决定是否继续；不复用旧页面按钮。
- 登录页先等待紫鸟填充；仅对候选唯一的邮箱 Continue、托管 Passkey、已填密码登录和
  已填 6 位 OTP 自动点击。每个页面动作最多 3 次，每个站点最多 3 轮完整验证；
  CA、UK、AU 的次数相互独立。
- 账户选择页先自动选定目标站点。自动处理仍未完成时按工作流和模式处理：审核流程保留人工验证入口；自动提现在未触发资金动作时可跳过该站点继续。建档 OTP 自动填充等待上限由 `onboarding-policy.toml` 配置，默认 15 秒。
- WebDriver 模式下由后台 `startBrowser` 自动弹出对应店铺的可见紫鸟窗口。只有自动处理
  停止后才显示人工继续入口；人工直接在该窗口验证，不打开紫鸟控制台。如果窗口未弹出或无法输入：后台取消 → 退出
  WebDriver → 普通紫鸟中维护该店铺登录 → 关闭普通紫鸟 → 重新运行
  `D:\Vibe Seller2\Ziniao-WebDriver.bat` → 重试。普通 Chrome 不访问 Seller Central。
- 页面适配器只接受 CA/UK/AU 固定域名以及已验证的
  `multi-row-card-row` / `kat-button` / `kat-link[label]` /
  `kat-statusindicator[label]` 契约；未知或歧义结构零点击。
- 飞书 App Secret 由 Windows Credential Manager 读取；SQLite 只保存凭据引用、
  App ID 与 Chat ID，日志不记录 Secret 或访问令牌。

启动时调用：

```python
await automation.recover_startup()
```

它优先按本地事实处理未决资金记录，不为资金回读打开浏览器；无资金 Guard 的 `QUEUED/RUNNING` 可恢复读取步骤，
丢失现场的 `WAITING_AUTH` 转为 `NEEDS_HUMAN_AUTH`，`WAITING_APPROVAL` 保持等待。
