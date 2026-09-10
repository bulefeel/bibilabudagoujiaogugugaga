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

import builtins
import dis
import importlib
import pkgutil
import re
from pathlib import Path

import ziniao_automation


SRC_ROOT = Path(ziniao_automation.__file__).resolve().parent


def _all_code_objects(code):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            yield from _all_code_objects(const)


def test_every_load_global_resolves_to_a_real_name() -> None:
    """引用了不存在的全局名 = 一颗只在错误分支爆炸的地雷。

    ``python -m compileall`` 和 import 都不查名字是否存在，NameError 要等运行
    到那一行才炸——而错误分支恰恰是测试最少走到的地方。这里对包里每个模块的
    字节码扫 LOAD_GLOBAL，名字必须在模块全局或 builtins 里。
    """

    missing: list[str] = []
    for info in pkgutil.walk_packages(ziniao_automation.__path__, "ziniao_automation."):
        module = importlib.import_module(info.name)
        source = getattr(module, "__file__", None)
        if not source:
            continue
        code = compile(
            Path(source).read_text(encoding="utf-8-sig"), source, "exec"
        )
        for code_object in _all_code_objects(code):
            for instruction in dis.get_instructions(code_object):
                if instruction.opname != "LOAD_GLOBAL":
                    continue
                name = instruction.argval
                if name in vars(module) or hasattr(builtins, name):
                    continue
                missing.append(f"{info.name}.{code_object.co_name}: {name}")
    assert not missing, f"引用了不存在的全局名：{sorted(set(missing))}"


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
