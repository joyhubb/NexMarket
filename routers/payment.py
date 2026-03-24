import os
import hmac
import hashlib
import requests

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

PAYOS_CLIENT_ID = os.getenv("PAYOS_CLIENT_ID")
PAYOS_API_KEY = os.getenv("PAYOS_API_KEY")
PAYOS_CHECKSUM_KEY = os.getenv("PAYOS_CHECKSUM_KEY")

PAYOS_URL = "https://api-merchant.payos.vn/v2/payment-requests"


class CreatePaymentRequest(BaseModel):
    amount: int = Field(ge=1000)
    orderCode: int


def create_signature(data: dict):

    raw_data = (
        f"amount={data['amount']}"
        f"&cancelUrl={data['cancelUrl']}"
        f"&description={data['description']}"
        f"&orderCode={data['orderCode']}"
        f"&returnUrl={data['returnUrl']}"
    )

    return hmac.new(
        PAYOS_CHECKSUM_KEY.encode(),
        raw_data.encode(),
        hashlib.sha256
    ).hexdigest()


@router.post("/create-payment")
def create_payment(req: CreatePaymentRequest):

    payload = {
        "orderCode": req.orderCode,
        "amount": req.amount,
        "description": str(req.orderCode)[:9],
        "cancelUrl": "https://marketcast.bubbleapps.io/version-test/cancel_transaction",
        "returnUrl": "https://marketcast.bubbleapps.io/version-test/payment_success"
    }

    payload["signature"] = create_signature(payload)

    headers = {
        "x-client-id": PAYOS_CLIENT_ID,
        "x-api-key": PAYOS_API_KEY,
        "Content-Type": "application/json"
    }

    response = requests.post(
        PAYOS_URL,
        json=payload,
        headers=headers,
        timeout=10
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()


@router.get("/check-payment/{orderCode}")
def check_payment(orderCode: int):

    url = f"{PAYOS_URL}/{orderCode}"

    headers = {
        "x-client-id": PAYOS_CLIENT_ID,
        "x-api-key": PAYOS_API_KEY,
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers, timeout=10)

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()