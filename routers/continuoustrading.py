from fastapi import APIRouter
from datetime import datetime
from typing import Optional
from pydantic import BaseModel

from engine import MatchingEngine, Order
from models import OrderSide, OrderType

router = APIRouter()
engine = MatchingEngine()


# ── Bubble request schema ────────────────────────────────────────────────────

class ContinuousMatchRequest(BaseModel):
    incoming_order_id: str
    incoming_user_id: str
    market_id: str            # symbol (e.g. "OPT1", "VNM")
    side: str                 # "BUY" or "SELL"
    price: float
    remaining_quantity: int
    created_at: str           # ISO-8601 e.g. "2026-04-08T10:00:00Z"


# ── Bubble response schema ───────────────────────────────────────────────────

class MatchedTrade(BaseModel):
    trade_id: str
    resting_order_id: str
    resting_user_id: Optional[str]
    price: float
    quantity: int


class ContinuousMatchResponse(BaseModel):
    incoming_order_id: str
    market_id: str
    side: str
    requested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    status: str               # "NEW" | "PARTIAL" | "FILLED"
    trades: list[MatchedTrade]
    message: str


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.post("/continuous-match", response_model=ContinuousMatchResponse)
def continuous_match(req: ContinuousMatchRequest):
    """
    Nhận lệnh từ Bubble và khớp theo ưu tiên giá-thời gian (Price-Time Priority).

    Quy tắc:
    - Ưu tiên 1 – GIÁ: bid cao nhất / ask thấp nhất được khớp trước.
    - Ưu tiên 2 – THỜI GIAN: cùng giá → lệnh vào sớm hơn (theo giây) được khớp trước.
    - Lệnh dư ATO đã có sẵn trong sổ với timestamp cũ → luôn có time priority
      cao hơn lệnh CT mới vào cùng mức giá.
    - Phần chưa khớp được giữ lại trong sổ lệnh.
    """

    # Parse timestamp từ Bubble (ISO-8601, có thể có "Z" hoặc "+00:00")
    try:
        submitted_at = datetime.fromisoformat(req.created_at.replace("Z", "+00:00"))
    except ValueError:
        submitted_at = datetime.utcnow()

    side = OrderSide.BUY if req.side.strip().upper() == "BUY" else OrderSide.SELL

    # Tạo Order nội bộ, dùng order_id của Bubble làm key
    order = Order(
        symbol=req.market_id,
        side=side,
        order_type=OrderType.LIMIT,
        price=req.price,
        quantity=req.remaining_quantity,
        account_id=req.incoming_user_id,
        submitted_at=submitted_at,
    )
    order.order_id = req.incoming_order_id
    engine._all_orders[order.order_id] = order

    # Chạy matching
    engine._match(order)

    # Phần còn dư → đưa vào sổ lệnh chờ khớp tiếp
    if order.remaining > 0:
        engine._book(order.symbol).add_order(order)

    # Build danh sách trade để trả về Bubble
    trades = [
        MatchedTrade(
            trade_id=f.trade_id,
            resting_order_id=f.matched_order_id,
            resting_user_id=_get_account_id(f.matched_order_id),
            price=f.price,
            quantity=f.quantity,
        )
        for f in order.fills
    ]

    # Thông điệp mô tả kết quả
    if order.remaining == 0:
        msg = "Order fully filled"
    elif order.filled_quantity > 0:
        msg = f"Partially filled {order.filled_quantity}/{req.remaining_quantity}, {order.remaining} units resting"
    else:
        msg = "No matching order found, resting in order book"

    return ContinuousMatchResponse(
        incoming_order_id=req.incoming_order_id,
        market_id=req.market_id,
        side=req.side.upper(),
        requested_quantity=req.remaining_quantity,
        filled_quantity=order.filled_quantity,
        remaining_quantity=order.remaining,
        status=order.status.value,
        trades=trades,
        message=msg,
    )


def _get_account_id(order_id: str) -> Optional[str]:
    o = engine._all_orders.get(order_id)
    return o.account_id if o else None
