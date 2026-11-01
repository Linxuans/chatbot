# -*- coding: utf-8 -*-
"""EnvFile 的读写测试：必须保住注释和顺序。"""
from __future__ import annotations

from wxagent.envfile import EnvFile

SAMPLE = """# 顶部注释
DEEPSEEK_MODEL=deepseek-flash
TEMPERATURE=0.7

# 分组注释
BOT_NAMES=城南
ENABLE_WEB_SEARCH=true
"""


def test_get_existing_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text(SAMPLE, encoding="utf-8")
    env = EnvFile(path)
    assert env.get("DEEPSEEK_MODEL") == "deepseek-flash"
    assert env.get("BOT_NAMES") == "城南"


def test_get_missing_key_returns_default(tmp_path):
    path = tmp_path / ".env"
    path.write_text(SAMPLE, encoding="utf-8")
    env = EnvFile(path)
    assert env.get("NOT_EXIST", "fallback") == "fallback"


def test_set_updates_in_place_and_keeps_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text(SAMPLE, encoding="utf-8")
    env = EnvFile(path)
    env.set("TEMPERATURE", "0.3")
    env.save()

    saved = path.read_text(encoding="utf-8")
    assert "# 顶部注释" in saved
    assert "# 分组注释" in saved
    assert "TEMPERATURE=0.3" in saved
    assert "DEEPSEEK_MODEL=deepseek-flash" in saved


def test_set_appends_missing_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text(SAMPLE, encoding="utf-8")
    env = EnvFile(path)
    env.set("UIA_POLL_INTERVAL", "3")
    env.save()

    saved = path.read_text(encoding="utf-8")
    assert "UIA_POLL_INTERVAL=3" in saved
    assert saved.rstrip().endswith("UIA_POLL_INTERVAL=3")


def test_as_dict_parses_all_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text(SAMPLE, encoding="utf-8")
    env = EnvFile(path)
    data = env.as_dict()
    assert data["BOT_NAMES"] == "城南"
    assert data["ENABLE_WEB_SEARCH"] == "true"
    assert "顶部注释" not in data


def test_missing_file_is_tolerated(tmp_path):
    env = EnvFile(tmp_path / "nope.env")
    assert env.get("ANY") == ""