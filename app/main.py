from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import init_db, get_db
from app.payment import process_payment, InsufficientFundsError, CardDeclinedError

app = FastAPI(title="FAILSAFE Payment API", version="0.1.0")


@app.on_event("startup")
def on_startup():
    init_db()


class PaymentRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class PaymentResponse(BaseModel):
    order_id: str
    charge_id: str
    user_id: str
    amount: float
    currency: str
    status: str


@app.post("/payment", response_model=PaymentResponse, status_code=201)
def create_payment(payload: PaymentRequest, db: Session = Depends(get_db)):
    try:
        result = process_payment(db, payload.user_id, payload.amount, payload.currency)
    except InsufficientFundsError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except CardDeclinedError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    return result
