from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime, timezone
from sortedcontainers import SortedList
import uuid

router = APIRouter()

BUY = "BUY"
SELL = "SELL"

STATUS_NEW = "NEW"
STATUS_PARTIAL = "PARTIAL"
STATUS_FILLED = "FILLED"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return _utc_now()


def _normalize_side(value: str) -> str:
    raw = str(value).strip().upper()
    if raw in ("BUY", "YES"):
        return BUY
    if raw in ("SELL", "NO"):
        return SELL
    raise ValueError(f"Invalid side: {value}")


def _bid_key(price: float, ts: datetime):
    return (-price, ts.timestamp())


def _ask_key(price: float, ts: datetime):
    return (price, ts.timestamp())


def _remaining(order: dict) -> int:
    return max(0, int(order["quantity"]) - int(order["filled"]))


def _status_from_values(filled: int, remaining: int) -> str:
    if remaining == 0:
        return STATUS_FILLED
    if filled > 0:
        return STATUS_PARTIAL
    return STATUS_NEW


class BubbleOrderPayload(BaseModel):
    order_id: str
    user_id: str
    option_id: str
    side: str
    price: float
    remain_quantity: int = Field(ge=0)
    created_date: str


class ContinuousMatchRequest(BaseModel):
    incoming_order: BubbleOrderPayload
    order_book: List[BubbleOrderPayload] = []


class MatchedTrade(BaseModel):
    trade_id: str
    resting_order_id: str
    resting_user_id: Optional[str] = None
    price: float
    quantity: int


class UpdatedOrder(BaseModel):
    order_id: str
    filled_quantity: int
    remaining_quantity: int
    status: Literal["NEW", "PARTIAL", "FILLED"]


class ContinuousMatchResponse(BaseModel):
    order_id: str
    option_id: str
    side: Literal["BUY", "SELL"]
    requested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    status: Literal["NEW", "PARTIAL", "FILLED"]
    trades: List[MatchedTrade]
    message: str
    updated_orders: List[UpdatedOrder] = []


def _build_internal_order(payload: BubbleOrderPayload) -> dict:
    return {
        "order_id": str(payload.order_id),
        "user_id": str(payload.user_id),
        "option_id": str(payload.option_id),
        "side": _normalize_side(payload.side),
        "price": float(payload.price),
        "quantity": int(payload.remain_quantity),
        "filled": 0,
        "submitted_at": _parse_datetime(payload.created_date),
    }


@router.post("/continuous-match", response_model=ContinuousMatchResponse)
def continuous_match(req: ContinuousMatchRequest):
    incoming = _build_internal_order(req.incoming_order)
    option_id = incoming["option_id"]

    bids: SortedList = SortedList(key=lambda x: x[0])
    asks: SortedList = SortedList(key=lambda x: x[0])
    orders: dict[str, dict] = {}

    def add_to_book(order: dict) -> None:
        if _remaining(order) <= 0:
            return
        ts = order["submitted_at"]
        if order["side"] == BUY:
            bids.add((_bid_key(order["price"], ts), order["order_id"]))
        else:
            asks.add((_ask_key(order["price"], ts), order["order_id"]))
        orders[order["order_id"]] = order

    def remove_from_book(order: dict) -> None:
        ts = order["submitted_at"]
        try:
            if order["side"] == BUY:
                bids.remove((_bid_key(order["price"], ts), order["order_id"]))
            else:
                asks.remove((_ask_key(order["price"], ts), order["order_id"]))
        except ValueError:
            pass

    for item in req.order_book:
        if item.option_id != option_id:
            continue
        if item.order_id == incoming["order_id"]:
            continue

        resting = _build_internal_order(item)
        if _remaining(resting) > 0:
            add_to_book(resting)

    trades = []

    while _remaining(incoming) > 0:
        if incoming["side"] == BUY:
            if len(asks) == 0:
                break
            _, resting_id = asks[0]
            resting = orders[resting_id]
            if incoming["price"] < resting["price"]:
                break
        else:
            if len(bids) == 0:
                break
            _, resting_id = bids[0]
            resting = orders[resting_id]
            if incoming["price"] > resting["price"]:
                break

        fill_qty = min(_remaining(incoming), _remaining(resting))
        trade_price = resting["price"]

        incoming["filled"] += fill_qty
        resting["filled"] += fill_qty

        trades.append({
            "trade_id": "T-" + str(uuid.uuid4())[:8].upper(),
            "resting_order_id": resting["order_id"],
            "resting_user_id": resting.get("user_id"),
            "price": trade_price,
            "quantity": fill_qty,
            "resting_filled_quantity": int(resting["filled"]),
            "resting_remaining_quantity": int(_remaining(resting)),
        })

        if _remaining(resting) == 0:
            remove_from_book(resting)

    filled = int(incoming["filled"])
    remaining = int(_remaining(incoming))
    requested = int(incoming["quantity"])
    status = _status_from_values(filled, remaining)

    if status == STATUS_FILLED:
        message = "Order fully filled"
    elif status == STATUS_PARTIAL:
        message = f"Partially filled {filled}/{requested}, {remaining} units resting in book"
    else:
        message = "No matching order found, order resting in book"

    updated_orders: List[UpdatedOrder] = []
    for t in trades:
        resting_filled = int(t["resting_filled_quantity"])
        resting_remaining = int(t["resting_remaining_quantity"])
        resting_status = _status_from_values(resting_filled, resting_remaining)

        updated_orders.append(
            UpdatedOrder(
                order_id=t["resting_order_id"],
                filled_quantity=resting_filled,
                remaining_quantity=resting_remaining,
                status=resting_status,
            )
        )

    return ContinuousMatchResponse(
        order_id=incoming["order_id"],
        option_id=incoming["option_id"],
        side=incoming["side"],
        requested_quantity=requested,
        filled_quantity=filled,
        remaining_quantity=remaining,
        status=status,
        trades=[
            MatchedTrade(
                trade_id=t["trade_id"],
                resting_order_id=t["resting_order_id"],
                resting_user_id=t["resting_user_id"],
                price=t["price"],
                quantity=t["quantity"],
            )
            for t in trades
        ],
        message=message,
        updated_orders=updated_orders,
    )
