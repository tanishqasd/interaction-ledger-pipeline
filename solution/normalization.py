"""Normalize CSV headers and typed values without touching storage."""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from unicodedata import decimal as digit_value

_MONTHS = {
    name: number for number, name in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1
    )
}
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_NUMERIC_DATE = re.compile(r"([0-9]{2})([/\-])([0-9]{2})\2([0-9]{4})")
_MONTH_NAME_DATE = re.compile(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{4})")
_CURRENCY = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_WHOLE = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,2}(?:,[0-9]{2})+,[0-9]{3})"
_AMOUNT = re.compile(rf"[+-]?(?:{_WHOLE}(?:\.[0-9]*)?|\.[0-9]+)")


def normalize_header(value: str) -> str:
    return re.sub(r"[_\s]+", " ", value.strip().lower())


def normalize_phone(value: str) -> str | None:
    digits = "".join(str(digit_value(char)) for char in value if char.isdecimal())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return f"+91{digits}"


def normalize_date(value: str) -> str | None:
    try:
        if _ISO_DATE.fullmatch(value):
            return date.fromisoformat(value).isoformat()
        if match := _NUMERIC_DATE.fullmatch(value):
            day, _, month, year = match.groups()
            return date(int(year), int(month), int(day)).isoformat()
        if match := _MONTH_NAME_DATE.fullmatch(value):
            day, month_name, year = match.groups()
            month = _MONTHS.get(month_name.lower())
            if month is not None:
                return date(int(year), month, int(day)).isoformat()
    except ValueError:
        pass
    return None


def normalize_amount(value: str) -> str | None:
    number = _CURRENCY.sub("", value)
    if not _AMOUNT.fullmatch(number):
        return None
    number = number.replace(",", "")
    amount = Decimal(number)
    if amount < 0:
        return None
    if amount.is_zero():
        return "0.00"
    with localcontext() as context:
        context.prec = max(context.prec, len(number) + 2)
        return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
