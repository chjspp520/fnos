"""静态自检：注册给 HA 事件/定时器的回调必须是 ``@callback`` 或 ``async``。

HA 对普通同步函数默认按 **Executor（线程）** 执行；这类回调里再调
``async_call_later`` / ``async_write_ha_state`` 等事件循环 API 会造成跨线程
竞态，严重时整个 Core 崩溃（RuntimeError: list changed size during iteration）。

运行 ``python tests/check_callbacks.py``。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 注册后会由 HA 调度的 API（方法名）
REGISTRARS = {
    "async_listen",
    "async_listen_once",
    "async_track_time_interval",
    "async_track_point_in_time",
    "async_dispatcher_connect",
    "async_call_later",
    "async_add_entities",
}


def _decorator_names(node):
    names = set()
    for deco in node.decorator_list:
        if isinstance(deco, ast.Name):
            names.add(deco.id)
        elif isinstance(deco, ast.Attribute):
            names.add(deco.attr)
        elif isinstance(deco, ast.Call):
            target = deco.func
            names.add(getattr(target, "id", None) or getattr(target, "attr", ""))
    return names


def _handler_name(arg):
    """从调用实参里取出被注册的函数名（self._x / _x / 无则 None）。"""
    if isinstance(arg, ast.Attribute):
        return arg.attr
    if isinstance(arg, ast.Name):
        return arg.id
    return None


def check_file(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(
                (isinstance(node, ast.AsyncFunctionDef),
                 "callback" in _decorator_names(node)))

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        api = getattr(func, "attr", None) or getattr(func, "id", None)
        if api not in REGISTRARS:
            continue
        for arg in node.args:
            name = _handler_name(arg)
            if not name or name not in funcs:
                continue
            variants = funcs[name]
            if any(is_async or is_callback
                   for is_async, is_callback in variants):
                continue
            problems.append((node.lineno, api, name))
    return problems


def main() -> None:
    failures = []
    for path in sorted(ROOT.glob("*.py")):
        for lineno, api, name in check_file(path):
            failures.append(f"{path.name}:{lineno} {api} 注册了同步函数 {name}（缺 @callback）")
    if failures:
        raise SystemExit("发现未加 @callback 的回调：\n" + "\n".join(failures))
    print("check_callbacks: OK")


if __name__ == "__main__":
    main()
