# 紫鸟多店铺本地自动化管理器 V1

本项目在 Windows 当前桌面会话中运行，管理后台仅监听
`http://127.0.0.1:8765`。SQLite、日志、截图、虚拟环境和缓存均保存在
`D:\Vibe Seller2\ziniao-automation`，不调用 AI 接口。

## 1. 首次安装

1. 双击 `Install.bat`。脚本优先使用项目内或 Vibe Seller 自带的 `uv.exe`。
2. `uv` 会把 Python 3.12 安装到项目 `.uv-python`，并在 D 盘建立 `.venv`；
   不依赖系统 `py` 或系统 Python。
3. 安装过程**不会**执行 `playwright install`，自动化直接连接紫鸟 Chromium。
4. 使用 `D:\Vibe Seller2\Ziniao-WebDriver.bat` 启动/切换紫鸟 WebDriver 模式。

## 2. 配置凭据

在 PowerShell 或 CMD 中执行：

```bat
cd /d "D:\Vibe Seller2\ziniao-automation"
.venv\Scripts\ziniao-automation.exe configure admin
.venv\Scripts\ziniao-automation.exe configure ziniao
.venv\Scripts\ziniao-automation.exe configure feishu
```

密码和 App Secret 使用隐藏输入。紫鸟密码、飞书 App Secret 存入 Windows
Credential Manager；SQLite 仅保存 `credential_ref`、App ID、Chat ID 等元数据。
命令和日志均不打印 Secret。

飞书应用需要机器人发消息权限，并将机器人加入目标群。`Chat ID` 通常以
`oc_` 开头。启用新系统前请轮换旧 App Secret。

## 3. 启动与停止

```bat
Start.bat
Stop.bat
```

浏览器打开 `http://127.0.0.1:8765`。`Start.bat` 会拒绝重复启动，
`Stop.bat` 只结束本项目记录的进程，不关闭紫鸟。

`Start.bat` 只启动本项目 D 盘虚拟环境中的
`pythonw.exe -m ziniao_automation.runner`。它不包含隐藏 PowerShell、编码命令、
下载器或自复制逻辑；后台 PID 由可审计的 Python 模块维护。早期脚本的隐藏启动命令链
容易触发安全软件误报，现已移除。

若安全软件仍保留旧记录，请确认当前文件路径和修改时间，重新扫描当前
`Start.bat`；不要恢复隔离区里的旧版本。

## 4. 登录时自动启动

双击 `Register-Startup.bat`。它注册当前 Windows 用户登录后的任务，使用当前交互桌面，
不会保存 Windows 登录密码。再次运行会安全更新任务。

## 5. 日志和清理

- JSONL 日志每日轮转，默认保留 90 天。
- 日志会递归遮盖 Password、Token、Cookie、Authorization、Secret 和 Webhook。
- 截图、报告、日志、备份可运行以下命令立即清理：

```bat
.venv\Scripts\ziniao-automation.exe cleanup --days 90
```

## 6. 小白排错

✅ **先检查**：紫鸟是否由 `Ziniao-WebDriver.bat` 启动，16851 是否监听。

✅ **需要登录验证**：点击身份检测或运行任务后，后台会调用紫鸟 `startBrowser`，自动
弹出对应店铺的可见窗口。程序会先等待紫鸟填充，再对唯一且明确的邮箱 Continue、托管
Passkey、已填密码登录和已填 6 位 OTP 执行自动点击；每个页面动作最多 3 次，每个站点
最多 3 轮，CA、UK、AU 分别计数。自动尝试用尽，或遇到未填内容、候选不唯一、CAPTCHA、
非托管 Passkey 等页面时，后台才显示人工继续入口。此时直接在原紫鸟窗口处理，完成后
回到本地后台点击“再次尝试自动登录并继续”；不要打开紫鸟控制台。

⚠️ **窗口未弹出或无法输入**：先在后台取消当前检测 → 退出 WebDriver 模式 → 在普通
紫鸟模式中打开对应店铺并完成登录/验证 → 关闭普通紫鸟及全部店铺窗口 → 重新运行
`D:\Vibe Seller2\Ziniao-WebDriver.bat` → 回到后台重试。普通 Chrome 只打开本地后台，
不要用它访问 Seller Central。

⚠️ **启动窗口闪退**：打开 `data\logs\ziniao-automation.jsonl`，查看最后一行的
`message`，不要复制 Cookie 或浏览器页面内容。

⚠️ **飞书不通知**：重新执行 `configure feishu`，确认机器人已进入目标群并有发消息权限。

💡 **完整流程说明**：见 `README-ZINIAO.md` 和 `WORKFLOW.md`。
