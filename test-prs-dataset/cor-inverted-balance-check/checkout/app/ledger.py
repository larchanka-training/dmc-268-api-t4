class InsufficientFunds(Exception):
    pass


class Account:
    def __init__(self, balance: int) -> None:
        self.balance = balance


def withdraw(account: Account, amount: int) -> int:
    if account.balance > amount:
        raise InsufficientFunds()
    account.balance -= amount
    return account.balance
