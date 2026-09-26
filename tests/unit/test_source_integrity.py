"""源码完整性扫描：抓「写完却没接上」和「编码毁掉」这两类静默缺陷。

0.6.3 的三处缺陷全部属于这两类，且 763 个既有测试一个都没抓到：

- ``amazon_login.py`` 的错误分支引用了不存在的名字 ``labelled`` —— 只有走到
  「按钮不唯一」那条路才 NameError，happy path 全绿；
- 满意度问卷的中文判据被写成 ``"???????"`` —— 语法完全合法，只是永远匹配不上
  中文页面，函数静默空转；
- ``startRunDetailPolling`` 定义了却没有任何调用点 —— 页面照常渲染，只是不刷新。

逐条断言字符串挡不住下一次（见 memory「断言不变量，别断言样本」），所以这里
按**类**扫：三条不变量分别是「每个被引用的全局名都存在」「源码里不允许出现
连续问号串」「app.js 里每个具名函数都至少被引用一次」。三个扫描器都在修复前
的真实缺陷上验证过会红。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import ziniao_automation


SRC_ROOT = Path(ziniao_automation.__file__).resolve().parent

# 扫描要 import 包里的每一个模块，而 ``main.py`` 是 ASGI 入口——**导入即副作用**：
# 按 ZINIAO_* 环境解析 Settings、创建 data 目录、把 jsonl 日志 handler 挂上根
# logger、还会按留存期清理 evidence/logs。在测试进程里直接导入，等于把这些副作用
# 打进真实项目目录（安装包构建的 stage 防线就是这么被触发的），之后整个测试套件
# 的日志都会灌进那个 handler。所以导入扫描放进子进程，并把环境指到临时目录。
_SCAN_SCRIPT = """
import builtins, dis, importlib, json, pkgutil, sys
from pathlib import Path
import ziniao_automation

def code_objects(code):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            yield from code_objects(const)

missing = []
for info in pkgutil.walk_packages(ziniao_automation.__path__, "ziniao_automation."):
    module = importlib.import_module(info.name)
    source = getattr(module, "__file__", None)
    if not source:
        continue
    code = compile(Path(source).read_text(encoding="utf-8-sig"), source, "exec")
    for code_object in code_objects(code):
        for instruction in dis.get_instructions(code_object):
            if instruction.opname != "LOAD_GLOBAL":
                continue
            name = instruction.argval
            if name in vars(module) or hasattr(builtins, name):
                continue
            missing.append(f"{info.name}.{code_object.co_name}: {name}")
print(json.dumps(sorted(set(missing))))
"""


def test_every_load_global_resolves_to_a_real_name(tmp_path: Path) -> None:
    """引用了不存在的全局名 = 一颗只在错误分支爆炸的地雷。

    ``python -m compileall`` 和 import 都不查名字是否存在，NameError 要等运行
    到那一行才炸——而错误分支恰恰是测试最少走到的地方。这里对包里每个模块的
    字节码扫 LOAD_GLOBAL，名字必须在模块全局或 builtins 里。
    """

    env = {
        **os.environ,
        "ZINIAO_PROJECT_ROOT": str(tmp_path),
        "ZINIAO_DATA_DIR": str(tmp_path / "data"),
        "ZINIAO_DATABASE_URL": f"sqlite:///{(tmp_path / 'scan.db').as_posix()}",
        "ZINIAO_TESTING": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-c", _SCAN_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"导入扫描本身失败：{completed.stderr[-2000:]}"
    missing = json.loads(completed.stdout.strip().splitlines()[-1])
    assert not missing, f"引用了不存在的全局名：{missing}"
    # 钉住沙箱确实接住了 main.py 的导入副作用：目录建在了 tmp 而不是项目里。
    # 这条断言失败 = ZINIAO_* 环境没生效，副作用又会去打真实项目目录（那正是
    # 安装包构建 stage\data 防线拦下的事故）。
    assert (tmp_path / "data" / "logs").is_dir(), "main.py 的副作用没有落进沙箱"


def test_no_mojibake_question_runs_in_source() -> None:
    """连续 3 个以上问号 = 中文字面量被错误编码毁掉的尸体。

    这类损坏语法照样合法、测试照样绿，但判据永远匹配不上真实页面，功能静默
    失效（同一坑已在 memory「Windows 打包踩坑」⑥记过一次）。正常代码没有理由
    出现 ``???``；真需要时改这条测试，而不是让它悄悄溜进去。
    """

    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*"):
        if path.suffix not in {".py", ".js", ".css", ".html"}:
            continue
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), 1
        ):
            if re.search(r"\?{3,}", line):
                offenders.append(
                    f"{path.relative_to(SRC_ROOT)}:{line_no}: {line.strip()[:60]}"
                )
    assert not offenders, f"疑似编码损坏的问号串：{offenders}"


def test_every_named_function_in_app_js_is_referenced() -> None:
    """app.js 里定义了却零引用的函数 = 写了功能忘了接线。

    一个 bundle 服务所有页面、全靠顶层初始化接线，所以「定义了没人调」不是
    死代码洁癖问题，而是功能整个没生效的信号（startRunDetailPolling 就是这样
    上线的：模板里容器都加了，函数从没被调用）。
    """

    source = (SRC_ROOT / "static" / "app.js").read_text(encoding="utf-8-sig")
    names = re.findall(r"^\s*function\s+([A-Za-z_$][\w$]*)\s*\(", source, re.MULTILINE)
    unreferenced = [
        name
        for name in sorted(set(names))
        if len(re.findall(rf"\b{re.escape(name)}\b", source)) < 2
    ]
    assert not unreferenced, f"定义了但没有任何调用点：{unreferenced}"


def test_wait_for_function_never_passes_its_argument_positionally() -> None:
    """Playwright 的 ``arg`` 是关键字参数，写成位置参数会当场 TypeError。

    2026-09-18 实测：四个调用点全部写成了位置参数，每一次都抛 TypeError 并被
    外层的 ``except Exception`` 吞掉。后果不是报错，而是**等待窗口静默失效**——
    登录助手 15 秒的邮箱/密码预填等待从未真正等过一次，紫鸟晚 5 秒填好密码就会
    被判成「紫鸟尚未填好 Amazon 登录密码」，交给人工。（OTP 走的是另一条轮询等待
    ``_wait_for_stable_otp``，不经过 ``wait_for_function``，那条没坏。）

    测试当时全绿，因为假替身把 ``arg`` 声明成了位置参数——替身与真实 API 的调用
    约定不一致，等于整块测试是假绿。所以这里按**类**扫源码，而不是逐个调用点
    断言：新写的第五个调用点也会被抓住。
    """

    import ast

    def aliases(tree: ast.AST) -> set[str]:
        """``wait = getattr(page, "wait_for_function", None)`` 之后调用的是 ``wait``。

        只认字面的方法名会漏掉这种绑定——本仓库四个调用点里有三个是这么写的，
        扫描器第一版就因此只抓到一个。
        """

        found = {"wait_for_function"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not (isinstance(call.func, ast.Name) and call.func.id == "getattr"):
                continue
            if len(call.args) < 2 or not isinstance(call.args[1], ast.Constant):
                continue
            if call.args[1].value != "wait_for_function":
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.add(target.id)
        return found

    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        # utf-8-sig：仓库里至少有一个模块带 BOM，用 utf-8 读会让 ast.parse 报
        # invalid non-printable character 而不是扫描出结果。
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        bound = aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute):
                matched = target.attr == "wait_for_function"
            elif isinstance(target, ast.Name):
                matched = target.id in bound
            else:
                matched = False
            if not matched:
                continue
            # 允许的形状只有一种：表达式是唯一的位置参数，其余全部具名。
            if len(node.args) > 1:
                offenders.append(
                    f"{path.relative_to(SRC_ROOT)}:{node.lineno} "
                    f"传了 {len(node.args)} 个位置参数"
                )
    assert not offenders, "wait_for_function 的 arg 必须具名传入：\n" + "\n".join(offenders)


def test_the_login_page_fake_matches_playwrights_real_signature() -> None:
    """替身的传参约定必须与 Playwright 一致，否则上面那类 bug 会重新变得不可见。"""

    import inspect

    from playwright.async_api import Page as RealPage

    from tests.unit.test_amazon_login_advancer import _Page as FakePage

    real = inspect.signature(RealPage.wait_for_function).parameters
    fake = inspect.signature(FakePage.wait_for_function).parameters
    for name in ("expression", "arg", "polling", "timeout"):
        assert name in fake, f"替身缺少参数 {name}"
        assert fake[name].kind is real[name].kind, (
            f"替身的 {name} 传参方式（{fake[name].kind}）"
            f"与 Playwright（{real[name].kind}）不一致"
        )
