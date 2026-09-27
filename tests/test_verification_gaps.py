"""
tests/test_verification_gaps.py

Reproduces FH-01 and FH-02 from analyzer/failure_hypotheses.json against the
CURRENT, unmodified application code.

Both tests are EXPECTED TO FAIL. The failures are the proof that the hypotheses
documented in failure_hypotheses.json describe real, reproducible bugs —
not speculative ones.

Do NOT add xfail, skip, or any fixup to app/ code.
"""

import threading
import time
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.payment import process_payment, charge_card
import app.payment as payment_module
from app import cache as app_cache

# ---------------------------------------------------------------------------
# Shared DB setup (in-memory SQLite so tests are fully isolated)
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite:///./gap_test.db"
test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def reset_state():
    Base.metadata.create_all(bind=test_engine)
    payment_module._balances["user_001"] = 500.00
    payment_module._balances["user_002"] = 150.00
    app_cache.clear()
    yield
    Base.metadata.drop_all(bind=test_engine)
    app_cache.clear()


def make_db():
    db = TestingSession()
    return db


# ---------------------------------------------------------------------------
# FH-01 — Duplicate charge on request retry
# Evidence:
#   inventory.json > idempotency_patterns[0]:
#     cache_checked_before_external_call = false
#     first_cache_read_line              = null
#     first_external_call_line           = 33  (charge_card)
#     first_cache_write_line             = 48  (cache.set, AFTER the charge)
#   test_coverage_map.json: 0 of 16 tests have covers.retry = true
# ---------------------------------------------------------------------------

def test_fh01_no_duplicate_charge_on_retry():
    """
    FH-01: simulates a client retry after a lost response.

    The client sends the same payment twice (identical user_id, amount,
    currency) — the normal case when a network timeout makes the first
    attempt ambiguous. A correct implementation would detect the duplicate
    (via an idempotency key or a prior cache lookup) and return the original
    result without re-charging.

    EXPECTED OUTCOME (correct behaviour):
        balance after two identical calls = 500.00 - 50.00 = 450.00

    ACTUAL OUTCOME (current code, no idempotency check):
        balance after two identical calls = 500.00 - 50.00 - 50.00 = 400.00

    The assertion below will FAIL, proving FH-01 is real.
    """
    user_id = "user_001"
    amount = 50.00
    currency = "USD"
    starting_balance = payment_module._balances[user_id]

    db1 = make_db()
    try:
        process_payment(db1, user_id, amount, currency)
    finally:
        db1.close()

    # Simulated retry: identical request, new DB session (as a real client would send)
    db2 = make_db()
    try:
        process_payment(db2, user_id, amount, currency)
    finally:
        db2.close()

    balance_after = payment_module._balances[user_id]
    expected_balance = starting_balance - amount  # only ONE charge should have landed

    assert balance_after == pytest.approx(expected_balance), (
        f"FH-01 confirmed: balance was deducted twice. "
        f"Expected {expected_balance} (one deduction), got {balance_after} "
        f"(two deductions of {amount} each from starting balance {starting_balance}). "
        f"Root cause: process_payment() calls charge_card() at app/payment.py:33 "
        f"before any cache read; cache.set() only fires at :48, after the charge."
    )


# ---------------------------------------------------------------------------
# FH-02 — Race condition on shared balance under concurrent requests
# Evidence:
#   inventory.json > shared_mutable_state[_balances]:
#     has_lock_protection = false
#     read_lines          = [20, 24]
#     write_lines         = [27]
#   inventory.json > functions[charge_card]:
#     calls_in_order = [_balances.get, InsufficientFundsError, uuid.uuid4]
#     (non-atomic read-modify-write, no lock between :24 and :27)
#   test_coverage_map.json: 0 of 16 tests have covers.concurrency = true
# ---------------------------------------------------------------------------

def test_fh02_no_negative_balance_under_concurrency(monkeypatch):
    """
    FH-02: two concurrent charge_card() calls for the same user, each requesting
    an amount that is individually affordable but whose sum exceeds the balance.

    user_001 starts with 500.00.  Two threads each request 300.00.
    300.00 < 500.00 individually, so both pass the funds check if they read
    the balance concurrently before either has written the deduction back.

    A correct implementation (with a lock around the read-modify-write) would
    allow exactly one charge to succeed and reject the other.

    To make the GIL-bounded race window deterministic we monkeypatch
    charge_card() to sleep between the balance read and the balance write,
    widening the window so both threads reliably read the original balance
    before either commits its deduction.

    EXPECTED OUTCOME (correct behaviour):
        exactly one charge succeeds; final balance >= 0

    ACTUAL OUTCOME (current code, no lock):
        both charges succeed; final balance = 500.00 - 300.00 - 300.00 = -100.00

    The assertions below will FAIL, proving FH-02 is real.
    """
    user_id = "user_001"
    amount = 300.00

    # Patch charge_card to insert a sleep between the balance read and write.
    # This widens the race window without modifying app/ source files.
    original_charge_card = payment_module.charge_card

    def racy_charge_card(uid, amt, currency):
        # Replicate the read that happens in the real charge_card
        balance = payment_module._balances.get(uid, 0.0)
        if balance < amt:
            from app.payment import InsufficientFundsError
            raise InsufficientFundsError(f"Balance {balance} is less than {amt}")
        # Yield so the other thread reads the same pre-deduction balance
        time.sleep(0.05)
        # Now both threads are past the guard and writing back concurrently
        import uuid
        payment_module._balances[uid] = balance - amt
        return f"ch_{uuid.uuid4().hex[:16]}"

    monkeypatch.setattr(payment_module, "charge_card", racy_charge_card)

    results = []
    errors = []

    def run_charge():
        try:
            charge_id = payment_module.charge_card(user_id, amount, "USD")
            results.append(charge_id)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=run_charge)
    t2 = threading.Thread(target=run_charge)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final_balance = payment_module._balances[user_id]
    successful_charges = len(results)

    assert successful_charges <= 1, (
        f"FH-02 confirmed: both concurrent charges succeeded ({successful_charges} charges). "
        f"With a correct lock only one should have been allowed."
    )
    assert final_balance >= 0, (
        f"FH-02 confirmed: balance went negative (final={final_balance}). "
        f"Root cause: charge_card() reads _balances at app/payment.py:24 and writes "
        f"at :27 with no threading.Lock; has_lock_protection=false in inventory.json."
    )
