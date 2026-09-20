"""secrets_store 加密存储单测。

锁定三条: 登记的字段落盘加密 / 未登记的既有字段(TickFlow/AI)行为不变 /
clear 不会把已加密字段的明文写回。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import secrets_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把 secrets.json 指向 tmp, 并在每个用例后清空。"""
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    yield tmp_path / "secrets.json"
    if (tmp_path / "secrets.json").exists():
        (tmp_path / "secrets.json").unlink()


def _on_disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_registered_field_is_encrypted_on_disk(store):
    secrets_store.save({"anspire_api_key": "sk-secret-value-1234"})
    disk = _on_disk(store)
    assert "sk-secret-value-1234" not in json.dumps(disk)
    if secrets_store._dpapi_available():
        assert disk["anspire_api_key"].startswith(secrets_store._ENC_TAG)


def test_registered_field_roundtrips(store):
    secrets_store.save({"anspire_api_key": "sk-secret-value-1234"})
    assert secrets_store.load()["anspire_api_key"] == "sk-secret-value-1234"
    assert secrets_store.get_env_backed_secret("anspire_api_key", "ANSPIRE_API_KEYS") == "sk-secret-value-1234"


def test_existing_fields_keep_plaintext_behavior(store):
    """TickFlow / AI Key 没登记加密, 行为与改造前完全一致。"""
    secrets_store.save({"tickflow_api_key": "tf-plain", "ai_api_key": "ai-plain"})
    disk = _on_disk(store)
    assert disk["tickflow_api_key"] == "tf-plain"
    assert disk["ai_api_key"] == "ai-plain"
    assert secrets_store.get_env_backed_secret("tickflow_api_key", "X") == "tf-plain"


def test_clear_does_not_write_back_plaintext(store):
    """回归: clear 曾直接写 load() 的明文结果, 会把加密字段降级成明文。"""
    secrets_store.save({"anspire_api_key": "sk-keep-me-9999", "tickflow_api_key": "tf"})
    secrets_store.clear("tickflow_api_key")
    disk = _on_disk(store)
    assert "sk-keep-me-9999" not in json.dumps(disk)   # 仍然加密
    assert secrets_store.get_env_backed_secret("anspire_api_key", "X") == "sk-keep-me-9999"


def test_clear_all_removes_file(store):
    secrets_store.save({"anspire_api_key": "x"})
    secrets_store.clear()
    assert not store.exists()


def test_unprotect_passthrough_for_legacy_plaintext():
    """历史明文(无加密标记)原样返回, 不报错。"""
    assert secrets_store._unprotect("legacy-plain-value") == "legacy-plain-value"


def test_mask_never_reveals_middle():
    masked = secrets_store.mask("sk-abcdefghijklmnop")
    assert masked.startswith("sk-a")
    assert masked.endswith("mnop")
    assert "cdefghijkl" not in masked
