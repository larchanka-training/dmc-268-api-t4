from app.totals import calculate_total


def checkout(cart):
    return calculate_total(cart.items, 0.2)
