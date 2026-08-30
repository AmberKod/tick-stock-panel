"""三段式 symbol 工具 (code + 市场后缀)。

M0 范围: CN 全量 + HK/US 的后缀识别与大小写归一;
M1 扩 is_valid 港股 5 位数字代码规则;
完整输入容错 (hk00700 / 700 / sh600519 等) 于 M1 末尾
移植 ai_stock_tools 项目的 symbols.py 实现。
"""
from __future__ import annotations

from app.markets.registry import resolve_market

_KNOWN_SUFFIXES = (".SH", ".SZ", ".BJ", ".HK", ".US")


def parse(symbol: str) -> tuple[str, str]:
    """拆出 (code, 后缀)。'600519.SH' → ('600519', '.SH');

    无已知后缀时原样返回 ('600519', '')。
    """
    value = str(symbol or "").strip()
    upper = value.upper()
    for suffix in _KNOWN_SUFFIXES:
        if upper.endswith(suffix):
            return value[: -len(suffix)], suffix
    return value, ""


def normalize(raw: str) -> str:
    """空白清理 + 后缀大小写归一。

    含已知后缀的输入只做归一、不做市场改写 (00700.HK 保持 HK 命名空间)。
    """
    value = str(raw or "").strip()
    if not value:
        return value
    upper = value.upper()
    for suffix in _KNOWN_SUFFIXES:
        if upper.endswith(suffix):
            return value[: -len(suffix)] + suffix
    return value


def is_valid(symbol: str) -> bool:
    """结构合法性: 必须有已知后缀。

    CN: 6 位纯数字代码 (沪/深/北 6 位)
    HK: 5 位纯数字代码 (港股 5 位 0 补齐)
    US: 1-5 位字母代码 (美股, M2 落地时启用)
    """
    code, suffix = parse(symbol)
    if not suffix or not code:
        return False
    market = resolve_market(symbol)
    if market == "CN":
        return len(code) == 6 and code.isdigit()
    if market == "HK":
        return len(code) == 5 and code.isdigit()
    # US (M2): 字母代码, 长度 1-5
    return code.isalpha() and 1 <= len(code) <= 5
