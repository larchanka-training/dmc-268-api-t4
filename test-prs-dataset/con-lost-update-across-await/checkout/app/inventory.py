from app.db import db


class OutOfStock(Exception):
    pass


async def reserve(sku: str, qty: int) -> int:
    stock = await db.get_stock(sku)
    if stock < qty:
        raise OutOfStock(sku)
    await db.set_stock(sku, stock - qty)
    return stock - qty
