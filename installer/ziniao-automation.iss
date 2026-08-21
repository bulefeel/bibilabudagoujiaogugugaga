; 紫鸟提现自动化 — Windows 安装包
;
; 用 installer\build.ps1 构建，不要直接在 IDE 里点编译：build.ps1 会先把源码
; 收集到一个干净的暂存目录，避免把 data\、.venv\、抽帧图片打进包里。
;
; 三条不能改的设计约束：
;
; 1. 装到 {localappdata}，PrivilegesRequired=lowest。免 UAC，同事双击就能装完。
;    数据也在同一棵树下——config.py 的 PROJECT_ROOT 是从包位置推出来的，
;    data/ 是 PROJECT_ROOT/data，这样零代码改动。
;
; 2. 源码装到 app\src，安装后用 uv pip install -e 做**可编辑安装**。
;    PROJECT_ROOT = Path(__file__).resolve().parents[2]，一旦改成普通安装，
;    parents[2] 会落进 site-packages，数据目录整个跑偏。
;
; 3. data\ 不在安装清单里（是运行时创建的），所以升级和卸载默认都不碰它。
;    里面是资金守卫记录，删掉就再也说不清哪笔钱发出去过。

#define AppName "紫鸟提现自动化"
#define AppId "ZiniaoAutomation"
#define AppPublisher "本地部署"
#define AppVersion "0.2.0"
#define StageDir "..\build\stage"

[Setup]
AppId={{8E4B6F21-3D7A-4C58-9A16-2F0B5D9C7E43}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppId}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes
; 全部装在当前用户名下，不需要管理员权限，也就不会弹 UAC。
PrivilegesRequired=lowest
OutputDir=..\build
; ASCII 文件名：中文名在下载链接、命令行传参和某些解压工具里都会出问题。
OutputBaseFilename=ZiniaoAutomation-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; uv 需要 64 位。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\app\installer\app.ico
SetupIconFile=app.ico

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"
Name: "startup"; Description: "开机自动启动后台服务（推荐，排期任务才能按时跑）"; GroupDescription: "启动方式:"

[Files]
; 整棵源码树。data\、.venv\ 等由 build.ps1 在暂存时排除。
Source: "{#StageDir}\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
; 先建出来并留给用户，卸载时不删——里面是数据库和证据截图。
Name: "{app}\app\data"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\app\.venv\Scripts\pythonw.exe"; Parameters: "-m ziniao_automation.launcher"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\installer\app.ico"; Comment: "打开管理后台"
Name: "{group}\停止后台服务"; Filename: "{app}\app\Stop.bat"; WorkingDir: "{app}\app"
Name: "{group}\重新安装运行环境"; Filename: "{app}\app\Install.bat"; WorkingDir: "{app}\app"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\app\.venv\Scripts\pythonw.exe"; Parameters: "-m ziniao_automation.launcher"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\installer\app.ico"; Tasks: desktopicon

[Run]
; 联网装 Python 3.12 与依赖。这一步最久，所以给它自己的进度提示。
Filename: "{cmd}"; Parameters: "/c """"{app}\app\Install.bat"""""; WorkingDir: "{app}\app"; StatusMsg: "正在安装运行环境（需要联网，约 2-5 分钟，请勿关闭窗口）..."; Flags: waituntilterminated
; 登录自启：直接执行 pythonw，不经 cmd /c、不隐藏窗口、不用编码命令。
; 这个项目被杀软误报过一次，起因就是隐藏 PowerShell 启动链。
Filename: "schtasks.exe"; Parameters: "/Create /F /SC ONLOGON /TN ""Ziniao Automation V1"" /TR ""\""{app}\app\.venv\Scripts\pythonw.exe\"" -m ziniao_automation.runner"""; Flags: runhidden waituntilterminated; Tasks: startup
Filename: "{app}\app\.venv\Scripts\pythonw.exe"; Parameters: "-m ziniao_automation.launcher"; WorkingDir: "{app}\app"; Description: "立即启动并打开管理后台"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; 先停服务再删文件，否则 .venv 里的 pythonw.exe 正被占用，卸载会留下残骸。
Filename: "{app}\app\Stop.bat"; Flags: runhidden waituntilterminated; RunOnceId: "StopService"
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""Ziniao Automation V1"""; Flags: runhidden waituntilterminated; RunOnceId: "DropStartupTask"

[Code]
var
  RemoveDataPage: TInputOptionWizardPage;

procedure InitializeWizard();
begin
  { 只在卸载时问；安装向导里不需要这一页。 }
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}\app\data');
    if DirExists(DataDir) then
    begin
      { 默认保留。这里面有资金守卫记录：哪笔提现在什么时候发出去过，
        删掉之后就再也无法回答。只有操作员明确选择才清空。 }
      if MsgBox('是否同时删除运行数据？' + #13#10 + #13#10 +
                '数据目录：' + DataDir + #13#10 + #13#10 +
                '里面包含提现记录、运行日志和证据截图。' + #13#10 +
                '如果以后还要用这台电脑跑提现，请选“否”保留。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
