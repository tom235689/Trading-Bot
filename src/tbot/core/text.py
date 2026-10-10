"""Number formats shared by messages and pages."""

import math


def price_text(price: float) -> str:
    """Two decimals, or six significant digits below 1, so 0.00001234 is not shown as 0.00."""
    if not math.isfinite(price) or price == 0 or abs(price) >= 1:
        return f"{price:,.2f}"
    text = f"{price:.{5 - math.floor(math.log10(abs(price)))}f}".rstrip("0")
    return text if len(text.split(".")[1]) >= 2 else f"{price:.2f}"
