# 构建安装包

```powershell
winget install --id JRSoftware.InnoSetup      # 只需一次
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

发布构建要求 Git 工作区干净，并产出：

- `build\ZiniaoAutomation-Setup-<版本>.exe`；
- 同名 `.sha256` 校验文件；
- `build\release-<版本>.json`（版本、Git 提交和依赖锁哈希）。

版本只在 `src\ziniao_automation\version.py` 中维护；构建脚本将同一个版本传给
Inno Setup。旧的 `0.2.1` 测试包若仍位于 `build\`，会移动到 `build\archive\`
保留，不会覆盖或删除。

## 为什么是这个形状

**Inno Setup，不是 PyInstaller onefile。** onefile 会把整个解释器解包到临时目录再
执行，这个行为模式和一类恶意软件相同，国内杀软误报率显著更高。这个项目已经因为
隐藏 PowerShell 启动链被误报过一次，不值得再冒一次险。Inno Setup 生成的是普通
安装包，行为可预期。

**装到 `%LOCALAPPDATA%`，`PrivilegesRequired=lowest`。** 免 UAC，同事双击就能装完。
数据也在同一棵树下——`config.py` 的 `PROJECT_ROOT` 是从包位置推出来的，`data/`
就是 `PROJECT_ROOT/data`，因此零代码改动。

**内置 `uv.exe`，联网装 Python 与依赖。** uv 是 bootstrap，缺了它什么都做不了，
所以必须随包走（压缩后约 11 MB，占了安装包的绝大部分）；Python 3.12 和依赖体积
大得多，交给网络。`Install.bat` 默认走清华 pypi 镜像，已设 `UV_DEFAULT_INDEX`
的机器保留自己的设置。

**装完执行 `uv sync --locked`（锁定依赖、可编辑安装）。** `uv.lock` 固定每个依赖
的版本和哈希，开发机和测试机不会因为安装日期不同而拿到不同版本；Playwright 仍只
安装 Python 包，不下载额外浏览器。
`PROJECT_ROOT = Path(__file__).resolve().parents[2]` —— 一旦改成普通安装，
`parents[2]` 会落进 `site-packages`，数据目录整个跑偏。这条不能改。

## 不能破坏的几件事

- **`data\` 不在安装清单里**。它是运行时创建的，所以升级和卸载默认都不碰它。
  里面是资金守卫记录（哪笔提现在什么时候发出去过），删掉就再也说不清。
  卸载时会单独询问是否删除，默认「否」。
- **登录自启直接执行 `pythonw.exe`**，不经 `cmd /c`、不隐藏窗口、不用编码命令。
  这正是当初触发杀软误报的写法，别改回去。
- **`build.ps1` 用白名单收集文件**，不是黑名单。黑名单迟早会漏掉某个新目录，
  而这个仓库里 `data\` 装的是真实资金记录和 Seller Central 截图。脚本末尾还有
  一道兜底断言，扫到 `.db`/`.jsonl`/`.log`/`.png` 就直接中止。
- **`build.ps1` 必须带 UTF-8 BOM**。Windows PowerShell 5.1 没有 BOM 时按 ANSI
  读取，中文字符串会被截断成语法错误。

## 代码签名

目前不签。未签名的安装包在 Win10 上会弹 SmartScreen「Windows 已保护你的电脑」，
需要点「更多信息」→「仍要运行」——README 里已经配了说明。

买了证书之后不用改脚本，传参即可：

```powershell
powershell -File installer\build.ps1 -CertificatePath cert.pfx -CertificatePassword ****
```

## 静默安装（批量部署 / 自动化验证）

```powershell
.\ZiniaoAutomation-Setup-0.6.2.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
    /DIR="C:\某个目录" /LOG=install.log
```

⚠️ 在 Git Bash 里跑要加 `MSYS_NO_PATHCONV=1`，否则 `/VERYSILENT` 会被 MSYS
当成路径改写成 `C:/Program Files/Git/VERYSILENT`，安装包会转成交互模式。

## 图标

`app.ico` 由 `installer/make_icon.py` 生成（纯 zlib + struct 手写 PNG/ICO，
不引 Pillow——为了一个只生成一次的图标加一个图像库不划算）。
