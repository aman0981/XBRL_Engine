"""iXBRL Transform Registry numeric/date format implementations.

Supports TR3 (REC 2015-02-26), TR4 (REC 2020-02-12), TR5 (REC 2022-02-16).
TR5 is the current primary; TR3 and TR4 retained for older filings.

Each transform returns the canonical lexical form expected by xbrli:decimal/integer/string
parsers, except numeric ones which return a `Decimal`.

Reference: https://www.xbrl.org/Specification/inlineXBRL-transformationRegistry/
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Callable


class TransformError(ValueError):
    pass


_NUM_DOT_PATTERN = re.compile(r"^[\s ]*-?[\d,]+(\.\d+)?[\s ]*$")
_NUM_COMMA_PATTERN = re.compile(r"^[\s ]*-?[\d.]+(,\d+)?[\s ]*$")
_WS_RE = re.compile(r"[\s ]")


def _strip(value: str) -> str:
    return _WS_RE.sub("", value).strip()


def _zerodash(value: str) -> Decimal:
    """ixt:zerodash / ixt5:zerodash: '-' or '–' (en-dash) -> 0."""
    cleaned = value.strip()
    if cleaned in ("-", "–", "—", ""):
        return Decimal(0)
    raise TransformError(f"zerodash expected dash, got {value!r}")


def _nocontent(value: str) -> Decimal:
    return Decimal(0)


def _fixed_zero(value: str) -> Decimal:
    return Decimal(0)


def _fixed_empty(value: str) -> str:
    return ""


def _fixed_true(value: str) -> str:
    return "true"


def _fixed_false(value: str) -> str:
    return "false"


def _num_dot_decimal(value: str) -> Decimal:
    """US/UK style: comma-grouped thousands, dot decimal. '1,234.56' -> 1234.56."""
    cleaned = _strip(value)
    if not cleaned:
        raise TransformError("empty")
    # Allow leading '(' '(123)' negative variants too — defensive (some filers slip).
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    cleaned = cleaned.replace(",", "")
    return Decimal(cleaned)


def _num_comma_decimal(value: str) -> Decimal:
    """EU style: dot-grouped thousands, comma decimal. '1.234,56' -> 1234.56."""
    cleaned = _strip(value)
    if not cleaned:
        raise TransformError("empty")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    cleaned = cleaned.replace(".", "").replace(",", ".")
    return Decimal(cleaned)


def _num_dot_decimal_apos(value: str) -> Decimal:
    """Apostrophe-grouped thousands, dot decimal (Swiss/Liechtenstein). '1'234.56' -> 1234.56."""
    cleaned = _strip(value).replace("'", "").replace("’", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return Decimal(cleaned)


def _num_unit_decimal(value: str) -> Decimal:
    """Whitespace-grouped thousands, comma decimal (FR/EU alt). '1 234,56' -> 1234.56."""
    cleaned = _strip(value).replace(",", ".")
    return Decimal(cleaned)


def _numdash(value: str) -> Decimal:
    """ixt3:numdash: '-' literal -> 0, otherwise pass numeric through dot-decimal."""
    stripped = value.strip()
    if stripped in ("-", "–"):
        return Decimal(0)
    return _num_dot_decimal(value)


_EN_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _num_words_en(value: str) -> Decimal:
    """ixt:numwordsen: 'thirty-five' -> 35. Conservative subset (1-99)."""
    cleaned = value.strip().lower().replace(",", "").replace("-", " ")
    if not cleaned:
        raise TransformError("empty")
    if cleaned == "no":
        return Decimal(0)
    total = 0
    for token in cleaned.split():
        if token in _EN_WORDS:
            total += _EN_WORDS[token]
        elif token in _TENS:
            total += _TENS[token]
        elif token == "hundred":
            total *= 100
        elif token == "thousand":
            total *= 1000
        elif token in ("and",):
            continue
        else:
            raise TransformError(f"numwordsen unknown token: {token}")
    return Decimal(total)


Transform = Callable[[str], Decimal | str]


# Format-qname suffix -> transform. Same suffix may appear under multiple TR namespaces.
# The dispatcher resolves namespace+suffix -> single canonical transform here.
TRANSFORMS: dict[str, Transform] = {
    "numdotdecimal": _num_dot_decimal,
    "num-dot-decimal": _num_dot_decimal,
    "numcommadecimal": _num_comma_decimal,
    "num-comma-decimal": _num_comma_decimal,
    "numdotdecimalin": _num_dot_decimal,
    "numdotdecimalapos": _num_dot_decimal_apos,
    "num-dot-decimal-apos": _num_dot_decimal_apos,
    "numunitdecimal": _num_unit_decimal,
    "num-unit-decimal": _num_unit_decimal,
    "numdash": _numdash,
    "zerodash": _zerodash,
    "nocontent": _nocontent,
    "no-content": _nocontent,
    "fixed-zero": _fixed_zero,
    "fixed-empty": _fixed_empty,
    "fixed-true": _fixed_true,
    "fixed-false": _fixed_false,
    "numwordsen": _num_words_en,
    "num-words-en": _num_words_en,
}


def transform_value(format_local: str, lexical: str) -> Decimal | str:
    """Apply transform identified by the local-name part of the format qname.

    Raises TransformError for unknown formats or malformed inputs.
    """
    if not format_local:
        return _num_dot_decimal(lexical)
    fn = TRANSFORMS.get(format_local)
    if fn is None:
        raise TransformError(f"unknown transform: {format_local}")
    return fn(lexical)


def apply_scale_and_sign(value: Decimal, scale: int | None, sign: str | None) -> Decimal:
    """xbrli:nonFraction `scale` and `sign` adjustments.

    - scale=N multiplies by 10**N (e.g. scale=3 turns 1.234 thousands into 1234).
    - sign='-' negates.
    """
    out = value
    if scale:
        out = out * (Decimal(10) ** scale)
    if sign == "-":
        out = -out
    return out
