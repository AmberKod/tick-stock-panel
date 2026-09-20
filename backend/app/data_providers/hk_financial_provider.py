"""Point-in-time Hong Kong ratios derived from dated, original disclosures.

The F10 aggregate valuation endpoint contains current share counts even on old
report rows. It is deliberately not a source for this provider. Financial values
come from the cumulative-period table in a dated announcement, including that
announcement's own comparative amounts.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx
import polars as pl

from app.data_providers.base import ProviderCapabilities

HK_RATIO_FIELDS = ("gross_margin", "net_margin", "revenue_yoy", "net_income_yoy")
HK_FINANCIAL_FIELDS = (*HK_RATIO_FIELDS, "roe", "debt_to_asset_ratio", "bps", "eps_ttm", "total_shares", "float_shares")
HK_FINANCIAL_SOURCE = "eastmoney_hk_announcement"
_INDEX_URL = "https://np-anotice-pc.eastmoney.com/api/security/ann"
_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
_SYMBOL = re.compile(r"^[0-9]{5}\.HK$")
_ART_CODE = re.compile(r"^AN[0-9]{12,30}$")
_DATE = re.compile(r"([0-9零\u3007○一二三四五六七八九]{4})年([0-9一二三四五六七八九十]{1,3})月([0-9一二三四五六七八九十]{1,3})日")
_CHINESE_DIGITS = str.maketrans("零〇○一二三四五六七八九", "000123456789")
_NUMBER = r"(?:\(?-?\d+(?:,\d{3})*(?:\.\d+)?\)?)"
_AMOUNT_ROW = re.compile(r"^\s*(.*?)\s+(" + _NUMBER + r")\s+(" + _NUMBER + r")(?:\s+" + _NUMBER + r"\s*[%\uff05])?\s*$")
_ROW_LABELS = {
    "revenue": {"收入", "收益", "營業收入", "营业收入", "營業額", "营业额", "Revenue", "Revenues", "Turnover"},
    "gross_profit": {"毛利", "毛利润", "毛利潤", "Grossprofit"},
    "net_profit": {"年度盈利", "期內盈利", "年度溢利", "期內溢利", "年內溢利", "年內盈利", "期内盈利", "年度利润", "期内利润", "淨利潤", "净利润", "Profitfortheyear", "Profitfortheperiod"},
    "parent_net_profit": {"本公司權益持有人應佔盈利", "本公司權益持有人應佔溢利", "本公司股東應佔溢利", "本公司股東應佔盈利", "本公司擁有人應佔溢利", "本公司擁有人應佔盈利", "母公司擁有人應佔溢利", "归属于母公司股东的净利润", "ProfitattributabletoownersoftheCompany"},
}
_FORMULAS = {
    "gross_margin": "gross_profit / revenue * 100",
    "net_margin": "net_profit / revenue * 100",
    "revenue_yoy": "(revenue / prior_revenue - 1) * 100",
    "net_income_yoy": "(parent_net_profit / prior_parent_net_profit - 1) * 100",
}


def financial_date(value: Any) -> date | None:
    """Parse a stated calendar date, without substituting an observation date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _chinese_number(value: str) -> int:
    translated = value.translate(_CHINESE_DIGITS)
    if "十" not in translated:
        return int(translated)
    tens, units = translated.split("十", 1)
    return int(tens or "1") * 10 + int(units or "0")


def _report_identity(title: str) -> tuple[date, int] | None:
    compact = re.sub(r"\s+", "", title)
    if not re.search(r"業績|业绩|results", compact, re.IGNORECASE):
        return None
    matches = list(_DATE.finditer(compact))
    if len(matches) != 1:
        return None
    try:
        period = date(*(_chinese_number(part) for part in matches[0].groups()))
    except ValueError:
        return None
    if re.search(r"九個月|九个月|ninemonths", compact, re.IGNORECASE):
        months = 9
    elif re.search(r"六個月|六个月|中期|半年|sixmonths", compact, re.IGNORECASE):
        months = 6
    elif re.search(r"全年|年度|annual|yearended", compact, re.IGNORECASE):
        months = 12
    elif re.search(r"三個月|三个月|季度|threemonths", compact, re.IGNORECASE):
        months = 3
    else:
        return None
    return period, months


def _currency_and_unit(text: str) -> tuple[str, str] | None:
    compact = re.sub(r"\s+", "", text).upper()
    for names, currency in ((r"人民幣|人民币|RMB|CNY", "CNY"), (r"港幣|港币|港元|HKD|HK\$", "HKD"), (r"美元|美金|USD|US\$", "USD")):
        if re.search(names, compact):
            if re.search(r"百萬|百万|MILLION", compact):
                return currency, f"{currency} million"
            if re.search(r"千元|千美元|千港元|THOUSAND|'000|\u2019000", compact):
                return currency, f"{currency} thousand"
            if re.search(r"萬元|万元", compact):
                return currency, f"{currency} ten_thousand"
    return None


def _as_number(value: str) -> float:
    number = float(value.strip("()").replace(",", ""))
    return -number if value.startswith("(") else number


def _tables(content: str, period: date, months: int) -> list[dict[str, Any]]:
    duration = {12: r"年度|YEAR", 9: r"九個月|九个月|NINEMONTHS", 6: r"六個月|六个月|SIXMONTHS", 3: r"三個月|三个月|THREEMONTHS"}[months]
    lines = content.splitlines()
    tables: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line).upper()
        if len(compact) > 65 or not re.search(r"截至|ENDED|ENDING", compact) or not re.search(duration, compact):
            continue
        following = lines[index + 1:index + 35]
        # Determine the two amount columns from the header itself. A reversed
        # comparative header is valid, but additional years/unknown columns are
        # not evidence that the first two numbers mean current/prior amounts.
        header_lines = []
        for candidate in following[:10]:
            prefix = re.match(r"^\s*(.*?)\s+(?=\(?-?\d)", candidate)
            if prefix and any(re.sub(r"\s+", "", prefix[1]).casefold() in {value.casefold() for value in labels} for labels in _ROW_LABELS.values()):
                break
            header_lines.append(candidate)
        header = "\n".join(header_lines).translate(_CHINESE_DIGITS)
        years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", header)
        if len(years) != 2 or set(years) != {str(period.year), str(period.year - 1)}:
            continue
        current_column = years.index(str(period.year))
        amount_unit = _currency_and_unit(header)
        if amount_unit is None:
            continue
        amounts: dict[str, tuple[float, float]] = {}
        original_lines: dict[str, str] = {}
        conflicting: set[str] = set()
        for candidate in following:
            normalized = re.sub(r"\s+", "", candidate)
            if re.search(r"非國際|非国际|NON.IFRS|NON.GAAP|經營資料|经营资料", normalized, re.IGNORECASE):
                break
            match = _AMOUNT_ROW.match(candidate)
            if match is None:
                continue
            label = re.sub(r"\s+", "", match[1])
            key = next((name for name, labels in _ROW_LABELS.items() if label.casefold() in {item.casefold() for item in labels}), None)
            if key is None:
                continue
            values = (_as_number(match[2]), _as_number(match[3]))
            pair = (values[current_column], values[1 - current_column])
            if key in amounts and amounts[key] != pair:
                conflicting.add(key)
            amounts[key] = pair
            original_lines[key] = candidate.strip()
        for key in conflicting:
            amounts.pop(key, None)
            original_lines.pop(key, None)
        if "revenue" in amounts:
            tables.append({"amounts": amounts, "lines": original_lines, "currency": amount_unit[0], "amount_unit": amount_unit[1], "header": compact + "\n" + header})
    return tables


def parse_hk_announcement(
    symbol: str,
    listing: Mapping[str, Any],
    detail: Mapping[str, Any],
    *,
    observed_at: str,
) -> dict[str, Any] | None:
    """Parse an unambiguous disclosed cumulative table; uncertain fields stay null."""
    if not _SYMBOL.fullmatch(symbol):
        return None
    code = str(listing.get("art_code", ""))
    if not _ART_CODE.fullmatch(code) or detail.get("art_code") != code:
        return None
    identities = listing.get("codes")
    if not isinstance(identities, list) or not any(isinstance(item, dict) and item.get("stock_code") == symbol[:5] and str(item.get("market_code")) == "116" for item in identities):
        return None
    announced = financial_date(listing.get("notice_date"))
    observed = financial_date(observed_at)
    if announced is None or observed is None or announced > observed or announced != financial_date(detail.get("notice_date")):
        return None
    identity = _report_identity(str(listing.get("title", "")))
    detail_identity = _report_identity(str(detail.get("notice_title", "")))
    if identity is None or detail_identity != identity:
        return None
    period, months = identity
    if period > announced:
        return None
    tables = _tables(str(detail.get("notice_content", "")), period, months)
    if not tables or len({item["currency"] for item in tables}) != 1:
        return None
    currency = tables[0]["currency"]
    values: dict[str, float | None] = dict.fromkeys(HK_FINANCIAL_FIELDS)
    provenance: dict[str, dict[str, Any]] = {}
    source_url = f"{_CONTENT_URL}?art_code={code}&client_source=web&page_index=1"
    for field in HK_RATIO_FIELDS:
        candidates: list[tuple[float, dict[str, Any]]] = []
        for table in tables:
            amounts = table["amounts"]
            revenue, prior_revenue = amounts["revenue"]
            value = None
            if field == "gross_margin" and "gross_profit" in amounts and revenue > 0:
                value = amounts["gross_profit"][0] / revenue * 100
            elif field == "net_margin" and "net_profit" in amounts and revenue > 0:
                value = amounts["net_profit"][0] / revenue * 100
            elif field == "revenue_yoy" and prior_revenue > 0:
                value = (revenue / prior_revenue - 1) * 100
            elif field == "net_income_yoy" and "parent_net_profit" in amounts and amounts["parent_net_profit"][1] > 0:
                value = (amounts["parent_net_profit"][0] / amounts["parent_net_profit"][1] - 1) * 100
            if value is not None and math.isfinite(value):
                candidates.append((value, table))
        if not candidates or any(not math.isclose(value, candidates[0][0], rel_tol=1e-12, abs_tol=1e-10) for value, _ in candidates):
            continue
        value, table = candidates[0]
        values[field] = value
        evidence = json.dumps({"header": table["header"], "amounts": table["amounts"], "unit": table["amount_unit"]}, sort_keys=True, ensure_ascii=False)
        provenance[field] = {
            "source": HK_FINANCIAL_SOURCE, "source_url": source_url, "announcement_id": code,
            "announce_date": announced.isoformat(), "unit": "percent_number", "currency": currency,
            "basis": "as_reported", "formula": _FORMULAS[field], "amount_unit": table["amount_unit"],
            "raw_fields": table["amounts"], "original_lines": table["lines"],
            "content_hash": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        }
    if not provenance:
        return None
    return {
        "symbol": symbol, "period_end": period, "announce_date": announced,
        "revision_id": code, "source": HK_FINANCIAL_SOURCE, "report_currency": currency,
        "observed_at": observed_at, "source_url": source_url, "publication_source": _INDEX_URL,
        "field_provenance": json.dumps(provenance, ensure_ascii=False, sort_keys=True),
        **values,
    }


def normalize_hk_financial_frame(frame: pl.DataFrame) -> pl.DataFrame:
    """Validate version identity and units before accepting provider/local history."""
    if frame.is_empty():
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    today = datetime.now(UTC).date()
    for raw in frame.iter_rows(named=True):
        symbol = str(raw.get("symbol", ""))
        period = financial_date(raw.get("period_end"))
        announced = financial_date(raw.get("announce_date"))
        if not _SYMBOL.fullmatch(symbol) or period is None or announced is None or period > announced or announced > today:
            continue
        if not all(raw.get(key) for key in ("revision_id", "source", "source_url", "publication_source")):
            continue
        try:
            provenance = json.loads(raw.get("field_provenance") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(provenance, dict):
            continue
        row = {key: raw.get(key) for key in ("revision_id", "source", "source_url", "publication_source", "observed_at", "report_currency")}
        accepted: dict[str, dict[str, Any]] = {}
        for field in HK_FINANCIAL_FIELDS:
            row[field] = None
            value = raw.get(field)
            info = provenance.get(field)
            if value is None or isinstance(value, bool) or not isinstance(info, dict):
                continue
            public_date = financial_date(info.get("announce_date"))
            expected_unit = "currency_per_share" if field in {"bps", "eps_ttm"} else "share" if field in {"total_shares", "float_shares"} else "percent_number"
            if public_date is None or public_date > today or info.get("unit") != expected_unit or info.get("basis") != "as_reported":
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            row[field] = number
            accepted[field] = dict(info)
            announced = max(announced, public_date)
        if accepted:
            row.update(symbol=symbol, period_end=period, announce_date=announced,
                       field_provenance=json.dumps(accepted, sort_keys=True, ensure_ascii=False))
            rows.append(row)
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None).with_columns([
        pl.col(field).cast(pl.Float64, strict=False) for field in HK_FINANCIAL_FIELDS
    ])


class HKFinancialProvider:
    """Read a bounded history of original HK announcements, without credentials."""

    name = "hk_financial"
    capabilities = ProviderCapabilities(financial=True)

    def __init__(self, transport: httpx.BaseTransport | None = None, timeout: float = 15.0) -> None:
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0))

    @staticmethod
    def _json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
        with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError("港股财务源响应超过大小限制")
                chunks.append(chunk)
        body = json.loads(b"".join(chunks))
        if not isinstance(body, dict) or body.get("success") not in (1, True):
            raise ValueError("港股财务源未返回有效公告数据")
        return body

    def _symbol_history(self, client: httpx.Client, symbol: str, latest_only: bool) -> list[dict[str, Any]]:
        today = datetime.now(UTC).date()
        start = today.replace(year=today.year - (2 if latest_only else 5), day=min(today.day, 28))
        listings: dict[str, dict[str, Any]] = {}
        for page in range(1, 21):
            body = self._json(client, _INDEX_URL, {"ann_type": "H", "market_stock_list": f"116.{symbol[:5]}",
                "client_source": "web", "business": "f10", "sr": -1, "page_size": 100,
                "page_index": page, "begin_time": start.isoformat(), "end_time": today.isoformat()})
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("港股财务公告列表结构无效")
            items = data["list"]
            for item in items:
                if isinstance(item, dict) and _report_identity(str(item.get("title", ""))) is not None and _ART_CODE.fullmatch(str(item.get("art_code", ""))):
                    listings[str(item["art_code"])] = item
            total = int(data.get("total_hits", len(items)))
            if page * 100 >= total:
                break
            if not items or page == 20:
                raise ValueError("港股财务公告分页覆盖不足或超过 2000 条上限")
        ordered = sorted(listings.values(), key=lambda item: (str(item.get("notice_date", "")), str(item["art_code"])), reverse=True)
        if len(ordered) > 32 and not latest_only:
            raise ValueError("港股财务报告数量超过单次 32 份上限")
        records: list[dict[str, Any]] = []
        observed = datetime.now(UTC).isoformat()
        for listing in ordered[:32]:
            combined: dict[str, Any] = {}
            row = None
            for page in range(1, 5):
                body = self._json(client, _CONTENT_URL, {"art_code": listing["art_code"], "client_source": "web", "page_index": page})
                detail = body.get("data")
                if not isinstance(detail, dict):
                    break
                if page == 1:
                    combined = dict(detail)
                else:
                    if detail.get("art_code") != combined.get("art_code") or detail.get("notice_date") != combined.get("notice_date"):
                        raise ValueError("港股财务公告分页版本不一致")
                    combined["notice_content"] = str(combined.get("notice_content", "")) + "\n" + str(detail.get("notice_content", ""))
                row = parse_hk_announcement(symbol, listing, combined, observed_at=observed)
                if row is not None and all(row.get(field) is not None for field in HK_RATIO_FIELDS):
                    break
                if page >= int(detail.get("page_size", 1)):
                    break
            if row is not None:
                records.append(row)
                if latest_only:
                    break
        return records

    def get_financials(self, table: str, symbols: list[str], latest_only: bool = False) -> pl.DataFrame:
        """Return original-report ratios; unsupported tables do not claim coverage."""
        if table != "metrics":
            return pl.DataFrame()
        if any(not _SYMBOL.fullmatch(symbol) for symbol in symbols):
            raise ValueError("港股历史财务只接受五位代码.HK")
        records: list[dict[str, Any]] = []
        with httpx.Client(transport=self._transport, timeout=self._timeout, follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}) as client:
            for symbol in dict.fromkeys(symbols):
                records.extend(self._symbol_history(client, symbol, latest_only))
        return normalize_hk_financial_frame(pl.DataFrame(records, infer_schema_length=None)) if records else pl.DataFrame()
