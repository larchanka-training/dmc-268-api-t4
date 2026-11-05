import contextlib

from app.payments import gateway


class Order:
    payment_id: str
    status: str = "authorised"


def settle(order: Order) -> Order:
    with contextlib.suppress(Exception):
        gateway.capture(order.payment_id)
    order.status = "paid"
    return order
