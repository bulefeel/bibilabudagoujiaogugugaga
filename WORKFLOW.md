# 固定工作流集成契约

V1 只允许代码注册的 `amazon_disbursement`，不加载网页上传脚本或动态模块。

```python
from ziniao_automation.workflows import (
    AutomationService,
    DatabaseRunLoader,
    SqlAlchemyWorkflowRepository,
    WorkflowEngine,
    WorkflowRegistry,
)
from ziniao_automation.workflows.amazon_disbursement import (
    AmazonDisbursementWorkflow,
    AmazonPaymentsPage,
)

repository = SqlAlchemyWorkflowRepository(session_factory)
workflow = AmazonDisbursementWorkflow(
    repository=repository,
    page_adapter=AmazonPaymentsPage(),
)
engine = WorkflowEngine(
    registry=WorkflowRegistry((workflow,)),
    repository=repository,
    browser_sessions=ziniao_controller,
    notifier=notifier,
)
loader = DatabaseRunLoader(session_factory, artifact_root=settings.evidence_dir)
automation = AutomationService(
    engine=engine,
    run_loader=loader,
    recovery_loader=loader.recovery_runs,
    ziniao_controller=ziniao_controller,
    session_factory=session_factory,
)
```

安全边界：

- `SqlAlchemyWorkflowRepository.arm_operation()` 在返回前提交 `ARMED`；之后才允许页面点击。
- 任何 `ARMED/SUBMITTED/UNCERTAIN` 记录都会把任务导向 `reconcile()`，不会再次调用提交按钮。
- 审核后重新读取域名、卖家 ID、付款账户、金额、结算周期及 DOM 指纹；任一变化会令审核失效。
- 登录页先等待紫鸟填充；仅对候选唯一的邮箱 Continue、托管 Passkey、已填密码登录和
  已填 6 位 OTP 自动点击。每个页面动作最多 3 次，每个站点最多 3 轮完整验证；
  CA、UK、AU 的次数相互独立。
- 自动次数用尽，或出现显式错误、未填内容、候选不唯一、CAPTCHA、账户选择、非托管
  Passkey 时停止自动点击，在紫鸟 `financial_session` 内等待人工处理 30 分钟并保留页面
  和锁；继续后重新执行完整检查。
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

它优先只回读未决资金记录；无资金 Guard 的 `QUEUED/RUNNING` 可安全恢复读取步骤，
丢失现场的 `WAITING_AUTH` 转为 `NEEDS_HUMAN_AUTH`，`WAITING_APPROVAL` 保持等待。
