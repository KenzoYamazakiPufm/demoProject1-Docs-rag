# -*- coding: utf-8 -*-
"""配置与部署相关用例。

重点守住两条云端部署依赖的行为：
1. `PKDB_SRC_DIR` 能覆盖数据源目录（云端没有 `F:\` 盘）
2. Streamlit Cloud 的 Secrets 能被桥接成环境变量（否则云端读不到 Key）
"""

from __future__ import annotations

import importlib
import os

import pytest

from pkdb import config


def test_default_source_dir_points_to_local_disk():
    """默认数据源是本地 Windows 绝对路径（可用环境变量覆盖）。"""
    assert config._DEFAULT_SRC_DIR
    assert os.path.isabs(config._DEFAULT_SRC_DIR)


def test_source_dir_env_override(monkeypatch):
    """PKDB_SRC_DIR 覆盖数据源目录后，source_available() 应当变成 False。"""
    fake = os.path.join(os.sep, "definitely", "not", "here")
    monkeypatch.setenv("PKDB_SRC_DIR", fake)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.SRC_DIR == fake
        assert reloaded.source_available() is False, "目录不存在时不得报可用"
        assert reloaded.source_docx_files() == []
    finally:
        monkeypatch.delenv("PKDB_SRC_DIR", raising=False)
        importlib.reload(config)


def test_source_available_reflects_real_dir():
    """本机数据源存在时应当为 True 且能列出全部切片。"""
    if not config.source_available():
        pytest.skip("本机没有数据源目录，跳过")
    assert len(config.source_docx_files()) == config.EXPECTED_DOC_COUNT


def test_streamlit_secrets_bridge_imports_strings_only(monkeypatch):
    """Streamlit Cloud 的 Secrets 只有**字符串**值会被桥接成环境变量。

    这是云端能读到 Key 的关键路径：云端 Secrets 只暴露在 st.secrets 里，
    不会自动进 os.environ。
    """
    st = pytest.importorskip("streamlit", reason="未安装 streamlit，跳过")

    # 用不可能与真实 .env 撞车的键名，避免"本来就在环境里"造成误判
    fake_secrets = {
        "PKDB_LLM_API_KEY": "sk-from-secrets",
        "PKDB_TEST_NONSTRING_INT": 7,          # 非字符串 -> 不桥接
        "PKDB_TEST_NONSTRING_DICT": {"a": 1},  # 非字符串 -> 不桥接
    }
    monkeypatch.setattr(st, "secrets", fake_secrets, raising=False)
    monkeypatch.delenv("PKDB_LLM_API_KEY", raising=False)

    config._bridge_streamlit_secrets()

    assert os.environ.get("PKDB_LLM_API_KEY") == "sk-from-secrets"
    assert "PKDB_TEST_NONSTRING_INT" not in os.environ
    assert "PKDB_TEST_NONSTRING_DICT" not in os.environ


def test_streamlit_secrets_bridge_never_overrides_real_env(monkeypatch):
    """真实环境变量优先：.env 里配好的值不该被 Secrets 覆盖。"""
    st = pytest.importorskip("streamlit", reason="未安装 streamlit，跳过")

    monkeypatch.setenv("PKDB_LLM_API_KEY", "sk-from-dotenv")
    monkeypatch.setattr(st, "secrets", {"PKDB_LLM_API_KEY": "sk-from-secrets"},
                        raising=False)

    config._bridge_streamlit_secrets()

    assert os.environ["PKDB_LLM_API_KEY"] == "sk-from-dotenv"


def test_llm_extra_body_parsed_from_json(monkeypatch):
    """PKDB_LLM_EXTRA_BODY 用"原样 JSON"透传厂商特性开关。

    首个真实用例就是关掉 DeepSeek 的思考模式（它默认打开）。
    """
    monkeypatch.setenv("PKDB_LLM_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')
    reloaded = importlib.reload(config)
    try:
        assert reloaded.LLM_EXTRA_BODY == {"thinking": {"type": "disabled"}}
        assert reloaded.LLM_EXTRA_BODY_ERROR == ""
    finally:
        monkeypatch.delenv("PKDB_LLM_EXTRA_BODY", raising=False)
        importlib.reload(config)


def test_llm_extra_body_invalid_json_is_reported_not_swallowed(monkeypatch):
    """JSON 填错**不能**被当成"没填"——那会造成最高危的假象：以为关了其实没关。"""
    monkeypatch.setenv("PKDB_LLM_EXTRA_BODY", "{thinking: disabled}")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.LLM_EXTRA_BODY == {}
        assert "JSON" in reloaded.LLM_EXTRA_BODY_ERROR
    finally:
        monkeypatch.delenv("PKDB_LLM_EXTRA_BODY", raising=False)
        importlib.reload(config)


def test_llm_extra_body_must_be_object(monkeypatch):
    """合法 JSON 但不是对象（如数组）同样要报错，不能悄悄忽略。"""
    monkeypatch.setenv("PKDB_LLM_EXTRA_BODY", '["thinking"]')
    reloaded = importlib.reload(config)
    try:
        assert reloaded.LLM_EXTRA_BODY == {}
        assert reloaded.LLM_EXTRA_BODY_ERROR
    finally:
        monkeypatch.delenv("PKDB_LLM_EXTRA_BODY", raising=False)
        importlib.reload(config)
