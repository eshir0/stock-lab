"""Share quantities: whole shares everywhere, and fractional US shares (four decimals, a small minimum order).

Money-handling code needs one place that decides what a valid quantity is and how it is rounded, so the ledger, the
sizing and the order limits agree. All arithmetic is done in Decimal; a quantity is stored as a plain int when it is a whole
number and as a float otherwise (JSON friendly, at most four decimals).

Fractional trading is a SIMULATION assumption for US instruments (Toss offers fractional US orders; its exact rules are not
modelled). Nothing here can place an order.
"""
import math
from decimal import Decimal, ROUND_FLOOR

STEP = Decimal('0.0001')          # the smallest fractional quantity
MIN_NOTIONAL = Decimal('1')       # smallest fractional order, in currency units (about one dollar)
MAX_QUANTITY = 10000


def dec(value):
    return Decimal(str(value))


def floor_to(value, fractional):
    """Round a non-negative amount of shares DOWN: to a whole share, or to four decimals when fractional."""
    value = dec(value)
    if value <= 0:
        return Decimal(0)
    step = STEP if fractional else Decimal(1)
    return (value/step).to_integral_value(rounding=ROUND_FLOOR)*step


def number(value):
    """A Decimal quantity as an int when whole, else a float with at most four decimals."""
    value = dec(value)
    return int(value) if value == value.to_integral_value() else float(value)


def is_valid(quantity, fractional):
    """True for a whole number of shares >= 1, or (fractional markets) a positive number with at most four decimals."""
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity):
        return False
    if quantity > MAX_QUANTITY:
        return False
    if isinstance(quantity, int):
        return quantity >= 1 or (fractional and quantity > 0)
    if not fractional:
        return False
    value = dec(quantity)
    return value > 0 and value == value.quantize(STEP)


def below_minimum(quantity, price, fractional):
    """A fractional order worth less than the minimum notional is refused (whole-share orders are never checked)."""
    return fractional and dec(quantity) != dec(quantity).to_integral_value() and dec(quantity)*dec(price) < MIN_NOTIONAL
