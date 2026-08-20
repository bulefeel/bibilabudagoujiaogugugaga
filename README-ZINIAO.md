# 紫鸟适配器契约

```python
from ziniao_automation.ziniao import ProfileSelector

selector = ProfileSelector("oauth", store.selector_value)
async with controller.financial_session(selector, store_key=str(store.id)) as handle:
    page = handle.page
```

- `ProfileSelector` 必须显式声明 `oauth` 或 `id`，数字 OAuth 不会被误判为 ID。
- `financial_session` 依次获取全局资金锁、店铺锁；V1 提现因此全部串行。
- 启动健康失败最多尝试 4 次，每次只对当前 selector 执行
  `stopBrowser -> startBrowser`，不重启共享紫鸟客户端。
- CDP 直接连接紫鸟 Chromium，不下载或启动 Playwright 浏览器。
- 真实 HTTP(S) 页面上无法确认 `navigator.webdriver` 时健康检查失败关闭；内部页为
  unknown，工作流仍须在资金操作前验证目标域名、登录和卖家身份。

自动登录只处理紫鸟已填充且候选唯一的登录控件，并遵循以下边界：

- 进入登录页后先等待 2～3 秒，再判断和点击；
- 邮箱 Continue、托管 Passkey、已填密码登录、已填 6 位 OTP 等每个页面动作最多自动
  点击 3 次；
- 每个站点最多进行 3 轮完整验证，CA、UK、AU 的次数按站点分别计算；
- 显式错误、未填内容、候选不唯一、CAPTCHA、账户选择或非托管 Passkey 会停止自动点击。

只有自动次数用尽或页面不再满足自动点击条件时，才进入人工验证。人工验证必须在上述
上下文内部等待，才能保留浏览器现场及锁：

```python
await controller.wait_for_auth(handle, auth_key=run.id, timeout_seconds=1800)
```

后台的“继续检查”和“取消”分别调用：

```python
await controller.continue_auth(run.id)
await controller.cancel_auth(run.id)
```

等待恢复后，工作流必须重新检查域名、卖家身份、付款账户和金额。超时会释放环境并抛
出 `AuthWaitExpired`。

## 自动登录与人工接管窗口

WebDriver 模式下不依赖紫鸟控制台手动开店。身份检测或工作流开始时，后台会根据该
店铺的 `browserOauth` 调用 `startBrowser`，由紫鸟自动弹出对应店铺的**可见窗口**。
程序会先按上面的有限次数自动推进。只有自动处理停止后，才在后台显示继续按钮；此时
直接在已经弹出的紫鸟窗口中处理剩余验证，再回到本地后台点击“再次尝试自动登录并继续”。
不要另外打开紫鸟控制台。

如果对应窗口没有弹出，或者自动弹出的窗口无法输入，按以下顺序维护登录状态：

1. 在本地后台取消当前检测或任务，释放对应店铺窗口；
2. 退出紫鸟 WebDriver 模式；
3. 在普通紫鸟模式中打开对应店铺，完成登录或验证；
4. 关闭普通紫鸟及其全部店铺窗口；
5. 重新运行 `D:\Vibe Seller2\Ziniao-WebDriver.bat`；
6. 回到 `http://127.0.0.1:8765` 重新发起检测。

普通 Chrome 只用于打开本地管理后台，整个流程都不用它访问 Seller Central。
