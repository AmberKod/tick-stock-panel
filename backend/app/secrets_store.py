"""Key / 凭据本地存储(§14)。

存储位置:`data/user_data/secrets.json`,权限 0600。
优先级:secrets.json > .env > 空(Free 模式)。

UI 改 Key 时只动这个文件,不动 .env。

加密(2026-09-18 新增):
  ENCRYPTED_FIELDS 里的字段落盘时经 DPAPI 加密(绑定当前 Windows 用户账户,
  换账户/换机器解不开), 读取时自动解密 —— 上层调用方无感。
  只对新接入的字段启用, 既有 tickflow/ai Key 保持原样, 避免动到已落盘的值。
  非 Windows 无等价 OS 级保护, 退化为明文 0600 并在首次读取时告警。
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 落盘加密的字段白名单 (新增敏感 Key 时登记这里)
ENCRYPTED_FIELDS = frozenset({
    "anspire_api_key",
    "bocha_api_key",
    "tavily_api_key",
    "brave_api_key",
    "serpapi_api_key",
})

_ENC_TAG = "__dpapi__"
_warned_plaintext = False


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi_bytes(payload: bytes, *, protect: bool) -> bytes:
    """调 Windows DPAPI (CryptProtectData / CryptUnprotectData)。

    用 ctypes 直连, 不引入 pywin32 依赖。失败一律抛 OSError, 由调用方降级。
    """
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    buf = ctypes.create_string_buffer(payload, len(payload))
    blob_in = DataBlob(len(payload), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    try:
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed "
                          f"(err={ctypes.get_last_error()})")
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


def _protect(value: str) -> str:
    """加密 → 带标记的 base64; 不可用时退化为明文并记录一次告警。"""
    global _warned_plaintext
    if _dpapi_available():
        try:
            blob = _dpapi_bytes(value.encode("utf-8"), protect=True)
            return _ENC_TAG + base64.b64encode(blob).decode("ascii")
        except Exception as e:
            logger.warning("secrets: DPAPI 加密失败, 该字段退化为明文: %s", e)
    if not _warned_plaintext:
        logger.warning("secrets: 当前平台无 OS 级凭据保护, 敏感 Key 以 0600 明文落盘")
        _warned_plaintext = True
    return value


def _unprotect(stored: str) -> str:
    """解密; 非加密值(历史明文)原样返回。"""
    if not isinstance(stored, str) or not stored.startswith(_ENC_TAG):
        return stored
    if not _dpapi_available():
        return stored
    try:
        return _dpapi_bytes(base64.b64decode(stored[len(_ENC_TAG):]), protect=False).decode("utf-8")
    except Exception as e:
        # 换了 Windows 账户/机器 → 解不开。返回原值让上层报"Key 无效",
        # 而不是静默当成空 Key 把功能关掉。
        logger.warning("secrets: DPAPI 解密失败(换账户或换机?): %s", e)
        return stored


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load() -> dict:
    """读盘并解密 ENCRYPTED_FIELDS, 返回明文 dict(调用方无感)。"""
    p = _path()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("secrets.json malformed: %s", e)
            return {}
        return {k: (_unprotect(v) if k in ENCRYPTED_FIELDS else v) for k, v in raw.items()}
    return {}


def _write_all(plain: dict) -> dict:
    """整份写盘(唯一落盘出口,保证 ENCRYPTED_FIELDS 一定被加密)。"""
    on_disk = {
        k: (_protect(v) if k in ENCRYPTED_FIELDS and isinstance(v, str) else v)
        for k, v in plain.items()
    }
    p = _path()
    p.write_text(json.dumps(on_disk, indent=2, ensure_ascii=False), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)
    return plain


def save(updates: dict) -> dict:
    """合并写入(不会清掉未提及的字段)。返回新内容(明文,便于回显/校验)。"""
    current = load()  # 已解密的明文
    current.update({k: v for k, v in updates.items() if v is not None})
    return _write_all(current)


def clear(*keys: str) -> dict:
    """清掉指定字段(留空清全部)。

    走 _write_all 而不是直接写盘: 否则会把已加密字段的**明文**写回去。
    """
    p = _path()
    if not p.exists():
        return {}
    if not keys:
        p.unlink()
        return {}
    current = load()
    for k in keys:
        current.pop(k, None)
    return _write_all(current)


def get_tickflow_key() -> str:
    """取当前 TickFlow Key:secrets.json 优先,否则 .env。"""
    val = load().get("tickflow_api_key")
    if val:
        return val
    from app.config import settings
    return settings.tickflow_api_key or ""


def get_ai_key() -> str:
    """取当前 AI Key:secrets.json 优先,否则 .env。"""
    val = load().get("ai_api_key")
    if val:
        return val
    from app.config import settings
    return settings.ai_api_key or ""


def get_ai_config(key: str, default: str = "") -> str:
    """取 AI 配置项:secrets.json 优先,否则 config。"""
    val = load().get(key)
    if val:
        return val
    from app.config import settings
    return getattr(settings, key, default) or default


def get_ai_config_int(key: str, default: int) -> int:
    """取 AI 数值配置项 (如 ai_max_output_tokens): secrets.json 优先,否则 config。"""
    val = load().get(key)
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            logger.warning("ai config %s is not an int: %r", key, val)
    from app.config import settings
    return int(getattr(settings, key, default) or default)


def get_env_backed_secret(field: str, env_name: str) -> str:
    """取环境变量后备的密钥(插件 API Key 等):secrets.json 优先,否则环境变量。

    与 get_tickflow_key 同优先级语义:UI 写入 secrets.json 后即覆盖 .env。
    """
    val = load().get(field)
    if val:
        return str(val).strip()
    return os.environ.get(env_name, "").strip()


def mask(key: str, prefix: int = 4, suffix: int = 4) -> str:
    """脱敏显示。"""
    if not key:
        return ""
    if len(key) <= prefix + suffix:
        return "•" * len(key)
    return f"{key[:prefix]}{'•' * 6}{key[-suffix:]}"
