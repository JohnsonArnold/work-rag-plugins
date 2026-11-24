"""Structured XLSX extraction and deterministic numeric operations."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import math
from zipfile import BadZipFile, ZipFile

from .contracts import CoreError, ErrorCode


def check_archive(path):
    with ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 128 * 1024**2:
            raise CoreError(ErrorCode.UNSUPPORTED, "Document expands beyond 128 MiB")


def value_of(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if type(value) is float and not math.isfinite(value):
        raise CoreError(ErrorCode.UNSUPPORTED, "Non-finite Excel number")
    return value


def extract(path):
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException
    from xml.etree.ElementTree import ParseError

    try:
        check_archive(path)
        with path.open("rb") as raw, path.open("rb") as values:
            formulas = load_workbook(raw, data_only=False, keep_links=False)
            try:
                cached = load_workbook(values, data_only=True, keep_links=False)
                try:
                    return _workbook(formulas, cached)
                finally:
                    cached.close()
            finally:
                formulas.close()
    except (BadZipFile, InvalidFileException, ParseError, KeyError, ValueError) as error:
        raise CoreError(ErrorCode.UNSUPPORTED, "Could not read this XLSX workbook") from error


def _workbook(formulas, cached):
    sheets = {}
    for sheet in formulas.worksheets:
        entries = {}
        unsupported = []
        # openpyxl's sparse cell mapping avoids iterating the entire declared dimension.
        for cell in sheet._cells.values():
            if cell.value is None:
                continue
            data = {"cell": cell.coordinate, "type": cell.data_type, "format": cell.number_format}
            if cell.data_type == "f":
                formula = cell.value
                if not isinstance(formula, str):
                    unsupported.append(cell.coordinate)
                    formula = getattr(formula, "text", None)
                stored = cached[sheet.title][cell.coordinate]
                data.update(formula=formula, cached=value_of(stored.value), cached_type=stored.data_type,
                            cache_state="missing" if stored.value is None else "freshness_unknown")
            else:
                data["value"] = value_of(cell.value)
            entries[cell.coordinate] = data
        sheets[sheet.title] = {
            "dimension": sheet.calculate_dimension(),
            "visibility": sheet.sheet_state,
            "merged": sorted(str(region) for region in sheet.merged_cells.ranges),
            "unsupported_formulas": unsupported,
            "cells": entries,
        }
    return {"epoch": formulas.epoch.date().isoformat(), "sheets": sheets,
            "coverage": "partial" if any(s["unsupported_formulas"] for s in sheets.values()) else "success"}


def number(item):
    if item is None:
        return None
    value = item.get("cached") if item["type"] == "f" else item.get("value")
    if value is None:
        return None
    if type(value) not in (int, float):
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Selection contains a nonnumeric cell", cell=item["cell"])
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Invalid number", cell=item["cell"]) from error


def calculate(items, operation, unit=None):
    if operation not in ("sum", "difference", "count", "average"):
        raise CoreError(ErrorCode.UNSUPPORTED, "Supported operations: sum, difference, count, average")
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Unit must be nonempty when supplied")
    numbers = [number(item) for item in items]
    included = [value for value in numbers if value is not None]
    if operation == "difference" and (len(numbers) != 2 or len(included) != 2):
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Difference needs exactly two numeric cells")
    if operation in ("sum", "average") and not included:
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Selection contains no numbers")
    with localcontext() as context:
        context.prec = 34
        if operation == "count":
            result = len(included)
        elif operation == "difference":
            result = included[0] - included[1]
        else:
            result = sum(included)
            if operation == "average":
                result /= len(included)
    return {"operation": operation, "value": result if isinstance(result, int) else str(result),
            "unit": unit, "precision": 34, "included": len(included),
            "missing": len(numbers) - len(included)}
