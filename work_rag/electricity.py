"""Rules for the two layouts in the electricity sample workbook."""
from datetime import date, datetime
from decimal import Decimal, localcontext
import re

from . import xlsx
from .contracts import CoreError, ErrorCode, Locator, Result


def _period(raw, epoch, year):
    from openpyxl.utils.datetime import from_excel

    if type(raw) in (int, float):
        parsed = from_excel(raw, epoch=datetime.fromisoformat(epoch))
        if isinstance(parsed, datetime):
            return [parsed.date().isoformat()] * 2
    if not isinstance(raw, str):
        return None
    try:
        day = date.fromisoformat(raw[:10])
        if len(raw) in (10, 19):
            return [day.isoformat()] * 2
    except ValueError:
        pass
    normalized = raw.replace("年", "/").replace("月", "/").replace("日", "").replace("—", "-")
    match = re.fullmatch(r"\s*(?:(\d{4})/)?(\d{1,2})/(\d{1,2})(?:\s*-\s*(\d{1,2})/(\d{1,2}))?\s*", normalized)
    if not match:
        return None
    explicit, a, b, c, d = match.groups()
    effective_year = int(explicit) if explicit else year
    if effective_year is None:
        return None
    try:
        first = date(effective_year, int(a), int(b))
        if c is None:
            return [first.isoformat()] * 2
        last = date(effective_year + ((int(c), int(d)) < (int(a), int(b))), int(c), int(d))
        return [first.isoformat(), last.isoformat()]
    except ValueError:
        return None


def analyze(knowledge, source_id, sheet, row, category=None, version_id=None, year=None):
    if type(row) is not int or not 3 <= row <= 1048575:
        raise CoreError(ErrorCode.INVALID_RANGE, "Provide a data row between 3 and 1048575")
    if year is not None and (type(year) is not int or not 1 <= year < 9999):
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Year must be explicit and between 1 and 9998")
    if sheet not in ("记录", "早+晚"):
        raise CoreError(ErrorCode.UNSUPPORTED, "This electricity rule requires 记录 or 早+晚")
    category = ("reading_interval" if sheet == "记录" else "day") if category is None else category
    version = knowledge.sources.version(source_id, version_id)
    artifact, book = knowledge._xlsx(version)
    if sheet not in book["sheets"]:
        raise CoreError(ErrorCode.NOT_FOUND, "Worksheet does not exist")
    cells = book["sheets"][sheet]["cells"]
    required_headers = {"A1": "日期", "E1": "日使用量-度", "H1": "倍率"} if sheet == "记录" else {
        "A1": "日期", "H1": "当天使用-度", "O1": "夜晚使用-度", "V1": "休息日使用-度"
    }
    if any(cells.get(key, {}).get("value") != value for key, value in required_headers.items()):
        raise CoreError(ErrorCode.UNSUPPORTED, "Worksheet headers do not match the electricity layout")
    raw_date = cells.get(f"A{row}", {}).get("value")
    if raw_date is None:
        raise CoreError(ErrorCode.NOT_FOUND, "Row has no date or interval")
    period = _period(raw_date, book["epoch"], year)
    warnings = []
    if period is None:
        warnings.append("Date interval is unresolved; provide a year only if its business context establishes it")
    warnings.append("Result covers one worksheet row and period; overlapping sheets or periods are not combined")
    if sheet == "记录" and category == "reading_interval":
        rules = [(f"{out}{row}", f"{col}{row+1}", f"{col}{row}", f"{mult}{row}")
                 for out, col, mult in zip("EFG", "BCD", "HIJ")]
    elif sheet == "早+晚" and category in ("day", "night"):
        if category == "day":
            rules = [(f"{out}{row}", f"{end}{row}", f"{start}{row}", multiplier)
                     for out, end, start, multiplier in zip("HIJ", "EFG", "BCD", (30, 30, 120))]
        else:
            rules = [(f"{out}{row}", f"{end}{row+1}", f"{start}{row}", multiplier)
                     for out, end, start, multiplier in zip("OPQ", "BCD", "EFG", (30, 30, 120))]
    elif sheet == "早+晚" and category == "holiday":
        rules = [(f"{out}{row}", None, None, None) for out in "VWX"]
    else:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Invalid period category for this sheet")
    meters, refs = [], {f"A{row}", *required_headers}
    total = Decimal(0)
    for meter, (out, end, start, multiplier) in zip(("west", "east", "hvac"), rules):
        target = cells.get(out)
        if end is None:
            if target is None or target["type"] == "f":
                raise CoreError(ErrorCode.AMBIGUOUS, "Holiday requires an explicit recorded value", cell=out)
            amount = xlsx.number(target)
            inputs = [out]
        else:
            factor = xlsx.number(cells.get(multiplier)) if isinstance(multiplier, str) else Decimal(multiplier)
            expected = f"=({end}-{start})*{multiplier}"
            if target is None or (target.get("formula") or "").replace("$", "").replace(" ", "") != expected:
                raise CoreError(ErrorCode.AMBIGUOUS, "Row formula does not match the known rule", cell=out)
            left, right = xlsx.number(cells.get(end)), xlsx.number(cells.get(start))
            if left is None or right is None or factor is None or factor <= 0:
                raise CoreError(ErrorCode.AMBIGUOUS, "Missing reading or invalid multiplier", cell=out)
            amount = (left - right) * factor
            inputs = [start, end] + ([multiplier] if isinstance(multiplier, str) else [])
        if amount is None or amount < 0:
            raise CoreError(ErrorCode.AMBIGUOUS, "Missing use or meter decrease", cell=out)
        total += amount
        refs.update([out, *inputs])
        meters.append({"meter": meter, "kwh": str(amount), "cell": out, "inputs": inputs,
                       "method": "recorded" if end is None else "meter_difference",
                       "workbook_cache": target.get("cached") if target else None})
    evidence = [knowledge._evidence(version, artifact, Locator("xlsx", sheet=sheet, cell_range=f"{ref}:{ref}"))
                for ref in sorted(refs)]
    return Result({"rule": "electricity-sample/1", "version_id": version.version_id, "date_raw": raw_date,
                   "period": period, "category": category, "unit": "kWh", "total": str(total), "meters": meters},
                  evidence=evidence, coverage="success" if period else "partial", warnings=warnings)


def total_periods(knowledge, source_id, sheet, rows, category=None, version_id=None, year=None):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Select 1 to 20 electricity rows")
    if any(type(row) is not int for row in rows) or len(set(rows)) != len(rows):
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Select distinct integer rows")
    version = knowledge.sources.version(source_id, version_id)
    if sheet == "记录":
        raise CoreError(ErrorCode.AMBIGUOUS, "Meter intervals in 记录 need verified start and end times; use row analysis")
    results = [analyze(knowledge, source_id, sheet, row, category, version.version_id, year) for row in rows]
    if any(item.data["period"] is None for item in results):
        raise CoreError(ErrorCode.AMBIGUOUS, "Resolve each selected period before totaling")
    periods = sorted((record.data["period"][0], record.data["period"][1], row)
                     for row, record in zip(rows, results))
    for previous, following in zip(periods, periods[1:]):
        if following[0] <= previous[1]:
            raise CoreError(ErrorCode.AMBIGUOUS, "Selected date ranges overlap", rows=[previous[2], following[2]])
    with localcontext() as context:
        context.prec = 34
        total = sum(Decimal(record.data["total"]) for record in results)
    evidence = list(dict.fromkeys(ref for record in results for ref in record.evidence))
    return Result({"rule": "electricity-sample/1", "version_id": version.version_id,
                   "category": results[0].data["category"], "unit": "kWh", "total": str(total),
                   "rows": [{"row": row, **record.data} for row, record in zip(rows, results)]},
                  evidence=evidence, coverage="success", warnings=["Total covers one sheet and one period category"])
