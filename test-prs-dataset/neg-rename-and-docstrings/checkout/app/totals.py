def calculate_total(items: list[dict], tax_rate: float) -> float:
    """Return the order total including tax, rounded to cents.

    Each item carries a unit `price` and a `qty`.
    """
    subtotal = 0.0
    for item in items:
        subtotal += item["price"] * item["qty"]
    return round(subtotal * (1 + tax_rate), 2)
