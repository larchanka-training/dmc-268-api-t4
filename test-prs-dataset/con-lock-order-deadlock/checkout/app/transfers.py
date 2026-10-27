import threading

accounts_lock = threading.Lock()
audit_lock = threading.Lock()
balances: dict[str, int] = {}
audit: list[str] = []


def transfer_out(account: str, amount: int) -> None:
    with accounts_lock:
        with audit_lock:
            balances[account] -= amount
            audit.append(f"out {account} {amount}")


def transfer_in(account: str, amount: int) -> None:
    with audit_lock:
        with accounts_lock:
            balances[account] += amount
            audit.append(f"in {account} {amount}")
