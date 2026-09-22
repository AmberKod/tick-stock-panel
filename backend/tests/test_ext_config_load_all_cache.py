"""ExtConfigStore.load_all 签名缓存测试 — 配置未变不重复读盘, 变更后立即可见, 返回副本。"""
from __future__ import annotations

import json

import pytest

from app.services import ext_data
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField


@pytest.fixture(autouse=True)
def _clean_cache():
    ext_data._load_all_cache.clear()
    yield
    ext_data._load_all_cache.clear()


def _patched_loads(monkeypatch, counter: dict):
    real_loads = json.loads

    def _counting(text):
        counter["loads"] += 1
        return real_loads(text)

    monkeypatch.setattr(ext_data.json, "loads", _counting)


def _config(cid: str, label: str) -> ExtConfig:
    return ExtConfig(id=cid, label=label, mode="snapshot",
                     fields=[ExtField(name="score", dtype="float")])


def test_second_load_all_hits_cache_without_disk_parse(tmp_path, monkeypatch):
    store = ExtConfigStore(tmp_path)
    store.upsert(_config("cfg_a", "A"))
    store.upsert(_config("cfg_b", "B"))

    counter = {"loads": 0}
    _patched_loads(monkeypatch, counter)
    first = store.load_all()
    assert counter["loads"] == 2, "缓存为空时逐 config.json parse"
    again = store.load_all()
    assert counter["loads"] == 2, "签名未变时不得重复读盘+parse"
    assert [c.id for c in first] == [c.id for c in again] == ["cfg_a", "cfg_b"]


def test_upsert_edit_invalidates_cache(tmp_path):
    store = ExtConfigStore(tmp_path)
    store.upsert(_config("cfg_a", "old"))
    assert store.load_all()[0].label == "old"

    store.upsert(_config("cfg_a", "new"))
    assert store.load_all()[0].label == "new"


def test_upsert_edit_invalidates_cache_even_when_dir_signature_unchanged(tmp_path):
    """同字节数覆写 + 落在同一 mtime 刻度 —— 目录签名逐字节相同, 仍必须读到新值。

    这是缓存失效的**确定性**回归用例。上面的 test_upsert_edit_invalidates_cache
    只是碰巧两次 upsert 落在了不同 mtime 刻度才绿(文件系统粒度一粗就会假绿),
    抓不到"签名不变"这个真正的失效盲区。这里用 os.utime 把 mtime 拨回缓存记录的
    同一刻度, 把触发条件钉死, 不依赖任何时序运气。

    修复前: 签名不变 -> 命中陈旧缓存, 返回 "AAA"(磁盘上其实是 "BBB")。
    """
    import os

    store = ExtConfigStore(tmp_path)
    store.upsert(_config("cfg_a", "AAA"))
    assert store.load_all()[0].label == "AAA"

    cp = tmp_path / "ext_data" / "cfg_a" / "config.json"
    cached = cp.stat()

    # 同字节数覆写(AAA -> BBB 长度相同), 再把 mtime 拨回缓存记录的同一刻度
    store.upsert(_config("cfg_a", "BBB"))
    os.utime(cp, ns=(cached.st_mtime_ns, cached.st_mtime_ns))

    # 前置条件自检: 确保真的构造出了"签名不变"的编辑, 否则本用例会退化成假绿
    assert cp.stat().st_size == cached.st_size, "前置条件: 覆写必须是同字节数"
    assert ext_data._ext_config_dir_signature(store._base) == (
        ("cfg_a", cached.st_mtime_ns, cached.st_size),
    ), "前置条件: 目录签名必须与缓存时逐字节一致"

    assert store.load_all()[0].label == "BBB"


def test_delete_invalidates_cache(tmp_path):
    store = ExtConfigStore(tmp_path)
    store.upsert(_config("cfg_a", "A"))
    store.upsert(_config("cfg_b", "B"))
    assert {c.id for c in store.load_all()} == {"cfg_a", "cfg_b"}

    assert store.delete("cfg_a") is True
    assert {c.id for c in store.load_all()} == {"cfg_b"}


def test_external_config_change_visible(tmp_path):
    import os

    store = ExtConfigStore(tmp_path)
    store.upsert(_config("cfg_a", "A"))
    assert store.load_all()[0].label == "A"

    cp = tmp_path / "ext_data" / "cfg_a" / "config.json"
    raw = json.loads(cp.read_text(encoding="utf-8"))
    raw["label"] = "externally-edited"
    cp.write_text(json.dumps(raw), encoding="utf-8")
    st = cp.stat()
    os.utime(cp, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert store.load_all()[0].label == "externally-edited"


def test_load_all_returns_copy_not_cached_object(tmp_path):
    store = ExtConfigStore(tmp_path)
    store.upsert(_config("cfg_a", "A"))
    first = store.load_all()
    first[0].label = "mutated"
    first[0].fields.append(ExtField(name="junk", dtype="string"))

    again = store.load_all()
    assert again[0].label == "A"
    assert [f.name for f in again[0].fields] == ["score"]


def test_load_all_without_config_dir_returns_empty(tmp_path):
    store = ExtConfigStore(tmp_path)
    assert store.load_all() == []
    assert store.load_all() == []
