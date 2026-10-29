# -*- coding: utf-8 -*-
"""UI Automation 界面树探测：看清微信窗口里到底有哪些控件。

用途：微信 4.x 的界面结构随版本变化，自动化代码不能靠猜。
先用这个脚本把真实控件树打出来，再照着写选择器。

    python scripts/uia_probe.py                 # 列出所有顶层窗口
    python scripts/uia_probe.py --title 微信     # dump 指定窗口的树
    python scripts/uia_probe.py --title 微信 --depth 6 --out tree.txt
"""
from __future__ import annotations

import argparse
import sys

import uiautomation as auto


def list_windows() -> None:
    root = auto.GetRootControl()
    print("=== 顶层窗口 ===")
    for index, child in enumerate(root.GetChildren()):
        try:
            rect = child.BoundingRectangle
            size = f"{rect.width()}x{rect.height()}" if rect else "?"
            print(
                f"[{index:02d}] name={child.Name!r} class={child.ClassName!r} "
                f"type={child.ControlTypeName} size={size} pid={child.ProcessId}"
            )
        except Exception as exc:  # 界面元素随时可能失效
            print(f"[{index:02d}] <读取失败: {exc}>")


def dump(window: auto.Control, max_depth: int, max_nodes: int) -> list[str]:
    lines: list[str] = []
    count = 0

    def walk(node: auto.Control, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_nodes:
            return
        count += 1
        try:
            rect = node.BoundingRectangle
            box = f"({rect.left},{rect.top},{rect.width()}x{rect.height()})" if rect else ""
            lines.append(
                f"{'  ' * depth}[{node.ControlTypeName}] name={node.Name!r} "
                f"class={node.ClassName!r} id={node.AutomationId!r} {box}"
            )
        except Exception as exc:
            lines.append(f"{'  ' * depth}<读取失败: {exc}>")
            return

        try:
            children = node.GetChildren()
        except Exception:
            return
        for child in children:
            walk(child, depth + 1)

    walk(window, 0)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="", help="窗口标题，支持部分匹配")
    parser.add_argument("--class-name", default="", help="窗口类名，部分匹配")
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--nodes", type=int, default=800)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if not args.title and not args.class_name:
        list_windows()
        return 0

    root = auto.GetRootControl()
    target = None
    for child in root.GetChildren():
        try:
            name = child.Name or ""
            cls = child.ClassName or ""
        except Exception:
            continue
        if args.title and args.title not in name:
            continue
        if args.class_name and args.class_name not in cls:
            continue
        target = child
        break

    if target is None:
        print("没找到匹配的窗口。先不带参数运行，看看有哪些窗口。", file=sys.stderr)
        return 1

    lines = dump(target, args.depth, args.nodes)
    text = "\n".join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已写入 {args.out}（{len(lines)} 个控件）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())