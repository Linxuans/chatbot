# -*- coding: utf-8 -*-
"""等待微信主窗口出现（登录成功），然后抓取界面树。

后台跑着，登录完成后自动落盘，方便据此编写选择器。
"""
from __future__ import annotations

import sys
import time

import uiautomation as auto


def find_main_window(timeout: float = 7200.0):
    """主窗口特征：mmui:: 类名 + 尺寸明显大于登录窗。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for child in auto.GetRootControl().GetChildren():
                cls = child.ClassName or ""
                if "mmui::" not in cls:
                    continue
                try:
                    rect = child.BoundingRectangle
                except Exception:
                    continue
                if rect and rect.width() > 700 and rect.height() > 400:
                    return child, cls
        except Exception:
            pass
        time.sleep(3)
    return None, ""


def dump(node, max_depth: int, max_nodes: int) -> list[str]:
    lines: list[str] = []
    state = {"n": 0}

    def walk(ctrl, depth: int) -> None:
        if depth > max_depth or state["n"] >= max_nodes:
            return
        state["n"] += 1
        try:
            rect = ctrl.BoundingRectangle
            box = f"({rect.left},{rect.top},{rect.width()}x{rect.height()})" if rect else ""
            lines.append(
                f"{'  ' * depth}[{ctrl.ControlTypeName}] name={ctrl.Name!r} "
                f"class={ctrl.ClassName!r} id={ctrl.AutomationId!r} {box}"
            )
        except Exception as exc:
            lines.append(f"{'  ' * depth}<读取失败: {exc}>")
            return
        try:
            for child in ctrl.GetChildren():
                walk(child, depth + 1)
        except Exception:
            return

    walk(node, 0)
    return lines


def main() -> int:
    print("等待微信主窗口出现（请扫码登录）…", flush=True)
    window, cls = find_main_window()
    if window is None:
        print("超时：没有检测到主窗口", file=sys.stderr)
        return 1

    print(f"检测到主窗口 class={cls}", flush=True)
    lines = dump(window, max_depth=8, max_nodes=1500)
    with open(".tmp/main_tree.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"已抓取 {len(lines)} 个控件 -> .tmp/main_tree.txt", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())