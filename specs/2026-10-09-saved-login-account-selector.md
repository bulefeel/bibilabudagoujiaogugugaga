# 已保存登录账号选择页支持（2026-10-09）

## 范围和现场依据
- 公共 AmazonLoginAdvancer 新增 saved_account_choice，提现、建档和 GPSR 复用。
- 在用户指定测试店铺的紫鸟现场，只读确认了 /ap/signin 的 cvf-account-switcher：
  switchableAccounts 卡片、switch_account_request 操作、POST /ap/switchaccount 容器。
- 这是登录账号选择，不是登录后的 /account-switcher 国家/店铺选择。
- 不使用店铺名、账号 ID、邮箱或完整登录 URL 作为生产条件。

## 转移契约
1. 复用调用方当前页面，不创建标签页、不猜测地址、不刷新或跳回首页。
2. 准确站点来源、/ap/signin 路径与可见列表结构同时满足才识别。
3. 默认选择当前 DOM 顺序下第一个可见的已有账号；多个账号同样选第一个。
4. 点击前同一 JS tick 重查结构、来源、控件状态和其他验证。
5. 添加账号、退出和隐藏残留不参与候选；第一项禁用不改选其他账号。
6. 正常跳转后回到既有密码、Passkey、OTP 或可信业务页处理；下游身份核验不变。
7. 保留当前每阶段最多三次预算、稳定等待和整体转移上限；超时或未知结构显式转人工。
8. 仅记录动作类别、数量和序号；不采集账号文本、data-value、登录链接查询参数或验证码。

## 测试方式调整
用户要求不在店铺浏览器中连续开关测试页面。旧的 CDP 样本回归入口已移除：
- 测试不再连接紫鸟，不调用 goto，不访问任何 Amazon 地址。
- 一个独立 headless 临时渲染器，只在 about:blank 上 set_content 脱敏 HTML。
- URL 为 JS 合约的内存输入，不是浏览器导航；离线模式和请求拦截共同禁用网络。
- 不读取个人浏览器资料，不使用店铺会话。
- 真实现场仅完成结构确认；尚未完成真实账号选择后的登录跳转验收。
- 本次未调用 GPSR 提交、提现及业务恢复接口。

## 变更
- src/ziniao_automation/workflows/amazon_saved_account.py：精确结构合约。
- src/ziniao_automation/workflows/amazon_login.py：分类、选择、重试和诊断。
- src/ziniao_automation/workflows/setup_diagnostics.py：白名单扩展。
- tests/unit/test_amazon_login_advancer.py：13 项新增状态机用例。
- tests/browser/test_amazon_saved_account.py：34 项纯离线 DOM 用例。
- tests/fixtures/amazon_login/saved_account_chooser.html：无真实身份的页面样本。

## 验证
- 登录单元与离线 DOM 回归：222 passed。
- 完整离线回归：1521 passed / 1 skipped（托盘环境相关）；493 条既有弃用警告。
- git diff --check：通过。
- 不构建 EXE、不提交或推送；后台重启已获得用户授权。

## 本地生效
- 后台由 PID 36548 重启为 36880；控制台 HTTP 200，16851 保持可达。
- /login 的 303 转到 / 是现有后台行为，跟随后返回 200。
- 重启前后无活动任务；没有恢复 GPSR、开启排期或执行资金业务。
- EXE 和 Git 远端未变，本次仅更新本地源码和后台。

## 2026-10-09 后续修正：页脚误判与初次跳转等待
- 实机报错并非补丁未加载：新分支在已稳定页面上仍返回 blocked。
- 只读结构证据：验证码、Passkey、错误、邮箱、密码和 OTP 控件均为 0。
- 唯一误判节点是列表外的普通帮助页脚，样式类 cvf-account-switcher-spacing-top-micro 命中了宽泛的 class 子串选择器。
- 修正：类名只作候选线索；额外账号选择必须具有明确选择器语义或可交互登录控件。已知帮助链接与纯排版容器不作为另一个验证流程；真实密码、验证码及额外账号控件继续拦截。
- 原样本漏掉页脚，上一轮离线通过未覆盖该结构。本轮补齐页脚，先复现 2 项失败再修正。
- 用户提出重定向时序问题；源码核对发现初次页面无可识别控件时立即转人工，而点击后才有等待。现初次进入也复用原有有界只读 settle 轮询，不增加 goto、刷新或固定长延时；明确验证码/错误仍立即退出。
- 同一真实页面修复前后只读结果：blocked → ready(count=1)，分类为 saved_account_choice；未点击账号、未导航。
- 新增回归覆盖页脚和排版变体、真实额外账号控件、延迟出现选择页、持续空白超时、等待期间验证码和域名变化。
- 本轮完整离线测试结果和部署状态以 data/recon/saved-account-login-blocker-20261009 中记录为准。

- 本轮全量离线回归：1532 passed / 1 skipped，493 条既有弃用警告。
- 用户授权后已完成本项目后台重启：PID 6996 → 29296，后台 HTTP 200；紫鸟客户端及 16851 端口保留正常。
- 重启前使用 SQLite Backup API 保存 pre-restart.db。原 GPSR 验证等待任务进入 NEEDS_HUMAN_AUTH，重启后无活动任务；未自动恢复或触发 GPSR 提交。
- 本次不构建 EXE、不提交或推送 Git。真实账号选择后的登录跳转仍待单次实测验收。
- 用户已授权后续修复验证通过后自行安排本项目后台重启，无需逐次确认；有正在执行的业务时先等待安全时机，不自动恢复业务提交。

## 用户验收与发布
- 2026-10-09：用户反馈重启后测试成功，并授权同步 GitHub 源码及 EXE 安装包。
- 真实登录验收结果来自用户反馈，本次发布不额外运行真实店铺任务。
- 修复随 0.7.1 补丁版发布，保留 0.7.0 的标签和安装包。
