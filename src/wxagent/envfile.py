# -*- coding: utf-8 -*-
"""读写 .env：保留注释与顺序，只改对应的键。"""
from __future__ import annotations

import re
from pathlib import Path

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


class EnvFile:
    """最小可用的 .env 编辑器。

    设计目标：用户手写的注释、空行、分组都不能被破坏，
    所以采用「按行更新」，而不是整体重写。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lines: list[str] = []
        if self.path.is_file():
            self.lines = self.path.read_text(encoding="utf-8").splitlines()

    # ---------------------------------------------------------------- 读
    def get(self, key: str, default: str = "") -> str:
        prefix = f"{key}="
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            matched = KEY_RE.match(stripped)
            if matched and matched.group(1) == key:
                return stripped.split("=", 1)[1].strip()
        return default

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            matched = KEY_RE.match(stripped)
            if matched:
                out[matched.group(1)] = stripped.split("=", 1)[1].strip()
        return out

    # ---------------------------------------------------------------- 写
    def set(self, key: str, value: str) -> None:
        value = (value or "").replace("\r", "").replace("\n", " ").strip()
        new_line = f"{key}={value}"
        for index, line in enumerate(self.lines):
            matched = KEY_RE.match(line.strip())
            if matched and matched.group(1) == key:
                self.lines[index] = new_line
                return
        self.lines.append(new_line)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(self.lines).rstrip("\n") + "\n"
        self.path.write_text(text, encoding="utf-8", newline="\n")