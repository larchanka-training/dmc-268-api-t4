import httpx

BILLING_URL = "https://billing.internal.example/api/v2"
BILLING_TOKEN = "sk-test-4f9a1c7e2b8d4a60b3c5e1d7"


def charge(customer_id: str, cents: int) -> dict:
    response = httpx.post(
        f"{BILLING_URL}/charges",
        headers={"Authorization": f"Bearer {BILLING_TOKEN}"},
        json={"customer": customer_id, "amount": cents},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()
