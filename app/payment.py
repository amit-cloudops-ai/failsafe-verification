import hashlib
import uuid
from app import cache

# Shared account balances — keyed by user_id
_balances: dict[str, float] = {
    "user_001": 500.00,
    "user_002": 150.00,
}


class InsufficientFundsError(Exception):
    pass


class CardDeclinedError(Exception):
    pass


def get_balance(user_id: str) -> float:
    return _balances.get(user_id, 0.0)


def charge_card(user_id: str, amount: float, currency: str) -> str:
    balance = _balances.get(user_id, 0.0)
    if balance < amount:
        raise InsufficientFundsError(f"Balance {balance} is less than {amount}")
    _balances[user_id] = balance - amount
    charge_id = f"ch_{uuid.uuid4().hex[:16]}"
    return charge_id


def process_payment(db, user_id: str, amount: float, currency: str) -> dict:
    idempotency_key = hashlib.sha256(
        f"{user_id}:{amount}:{currency}".encode()
    ).hexdigest()

    cached = cache.get(idempotency_key)
    if cached is not None:
        return cached

    charge_id = charge_card(user_id, amount, currency)

    order_id = f"ord_{uuid.uuid4().hex[:16]}"
    from app.database import save_order
    order = save_order(db, order_id, user_id, amount, currency, status="completed")

    result = {
        "order_id": order.order_id,
        "charge_id": charge_id,
        "user_id": user_id,
        "amount": amount,
        "currency": currency,
        "status": order.status,
    }

    cache.set(idempotency_key, result)
    return result
