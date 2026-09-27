from __future__ import annotations

from satrap.core.utils.proc_env import sanitized_child_env


def test_strips_suffix_matched_sensitive_vars(monkeypatch):
    """名称以 KEY/TOKEN/SECRET/PASSWORD 等为完整尾部分段的变量应被剥离"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("SATRAP_API_TOKEN", "t")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("TOKEN", "bare")
    monkeypatch.setenv("service_api_key", "lowercase")

    env = sanitized_child_env()

    assert "OPENAI_API_KEY" not in env
    assert "SATRAP_API_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DB_PASSWORD" not in env
    assert "TOKEN" not in env
    assert "service_api_key" not in env


def test_keeps_normal_vars_containing_sensitive_prefix(monkeypatch):
    """含敏感词前缀但尾部分段不同的正常变量不应被误删"""
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    monkeypatch.setenv("SECRET_PROJECT_FOLDER", "x")
    monkeypatch.setenv("PATH", "C:/bin")

    env = sanitized_child_env()

    assert env["TOKENIZERS_PARALLELISM"] == "false"
    assert env["SECRET_PROJECT_FOLDER"] == "x"
    assert "PATH" in env


def test_allow_list_restores_explicit_vars(monkeypatch):
    """allow 显式放行的变量即使命中敏感特征也保留"""
    monkeypatch.setenv("TEST_SERVICE_TOKEN", "v")

    env = sanitized_child_env(allow=["TEST_SERVICE_TOKEN"])

    assert env["TEST_SERVICE_TOKEN"] == "v"


def test_overrides_win_over_stripped_values(monkeypatch):
    """overrides 追加的变量优先于剥敏结果"""
    monkeypatch.setenv("MY_TOKEN", "secret")

    env = sanitized_child_env(MY_TOKEN="safe")

    assert env["MY_TOKEN"] == "safe"
