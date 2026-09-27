import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db
import app.payment as payment_module
from app import cache as app_cache

TEST_DATABASE_URL = "sqlite:///./test_failsafe.db"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    app.dependency_overrides[get_db] = override_get_db
    # Reset balances and idempotency cache to known state before each test
    payment_module._balances["user_001"] = 500.00
    payment_module._balances["user_002"] = 150.00
    app_cache.clear()
    yield
    Base.metadata.drop_all(bind=test_engine)
    app.dependency_overrides.clear()
    app_cache.clear()


@pytest.fixture
def client():
    return TestClient(app)


class TestSuccessfulPayment:
    def test_returns_201(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": 50.0, "currency": "USD"})
        assert response.status_code == 201

    def test_response_shape(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": 50.0, "currency": "USD"})
        body = response.json()
        assert "order_id" in body
        assert "charge_id" in body
        assert body["user_id"] == "user_001"
        assert body["amount"] == 50.0
        assert body["currency"] == "USD"
        assert body["status"] == "completed"

    def test_order_id_prefixed(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": 10.0})
        assert response.json()["order_id"].startswith("ord_")

    def test_charge_id_prefixed(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": 10.0})
        assert response.json()["charge_id"].startswith("ch_")

    def test_balance_decremented(self, client):
        client.post("/payment", json={"user_id": "user_001", "amount": 100.0})
        assert payment_module._balances["user_001"] == pytest.approx(400.0)

    def test_default_currency_is_usd(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": 25.0})
        assert response.json()["currency"] == "USD"

    def test_explicit_currency_preserved(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": 25.0, "currency": "EUR"})
        assert response.json()["currency"] == "EUR"

    def test_exact_balance_accepted(self, client):
        response = client.post("/payment", json={"user_id": "user_002", "amount": 150.0})
        assert response.status_code == 201


class TestInsufficientFunds:
    def test_returns_402(self, client):
        response = client.post("/payment", json={"user_id": "user_002", "amount": 999.0})
        assert response.status_code == 402

    def test_error_detail_present(self, client):
        response = client.post("/payment", json={"user_id": "user_002", "amount": 999.0})
        assert "detail" in response.json()

    def test_balance_unchanged_on_failure(self, client):
        client.post("/payment", json={"user_id": "user_002", "amount": 999.0})
        assert payment_module._balances["user_002"] == pytest.approx(150.0)

    def test_unknown_user_has_zero_balance(self, client):
        response = client.post("/payment", json={"user_id": "ghost_user", "amount": 1.0})
        assert response.status_code == 402


class TestInputValidation:
    def test_negative_amount_rejected(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": -5.0})
        assert response.status_code == 422

    def test_zero_amount_rejected(self, client):
        response = client.post("/payment", json={"user_id": "user_001", "amount": 0})
        assert response.status_code == 422

    def test_missing_user_id_rejected(self, client):
        response = client.post("/payment", json={"amount": 10.0})
        assert response.status_code == 422

    def test_missing_amount_rejected(self, client):
        response = client.post("/payment", json={"user_id": "user_001"})
        assert response.status_code == 422
