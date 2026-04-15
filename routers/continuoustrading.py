from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List, Literal
from datetime import datetime, timezone
from sortedcontainers import SortedList
import uuid

router = APIRouter()

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
BUY = "BUY"
SELL = "SELL"

STATUS_NEW = "NEW"
STATUS_PARTIAL = "PARTIAL"
STATUS_FILLED = "FILLED"

# ─────────────────────────────────────────────
# In-memory order book (per option_id)
# key = option_id
# ─────────────────────────────────────────────
_bids: dict[str, SortedList] = {}
_asks: dict[str, SortedList] = {}
_orders: dict[str, dict] = {}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
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


def _get_bids(option_id: str) -> SortedList:
    if option_id not in _bids:
        _bids[option_id] = SortedList(key=lambda x: x[0])
    return _bids[option_id]


def _get_asks(option_id: str) -> SortedList:
    if option_id not in _asks:
        _asks[option_id] = SortedList(key=lambda x: x[0])
    return _asks[option_id]


def _bid_key(price: float, ts: datetime):
    # higher price first, earlier time first
    return (-price, ts.timestamp())


def _ask_key(price: float, ts: datetime):
    # lower price first, earlier time first
    return (price, ts.timestamp())


def _remaining(order: dict) -> int:
    return max(0, int(order["quantity"]) - int(order["filled"]))


def _add_to_book(order: dict) -> None:
    if _remaining(order) <= 0:
        return

    ts = order["submitted_at"]
    oid = order["option_id"]

    if order["side"] == BUY:
        _get_bids(oid).add((_bid_key(order["price"], ts), order["order_id"]))
    else:
        _get_asks(oid).add((_ask_key(order["price"], ts), order["order_id"]))

    _orders[order["order_id"]] = order


def _remove_from_book(order: dict) -> None:
    ts = order["submitted_at"]
    oid = order["option_id"]

    try:
        if order["side"] == BUY:
            _get_bids(oid).remove((_bid_key(order["price"], ts), order["order_id"]))
        else:
            _get_asks(oid).remove((_ask_key(order["price"], ts), order["order_id"]))
    except ValueError:
        pass


def _get_order_status(order: dict) -> str:
    remaining = _remaining(order)
    filled = int(order["filled"])

    if remaining == 0:
        return STATUS_FILLED
    if filled > 0:
        return STATUS_PARTIAL
    return STATUS_NEW


def _match(incoming: dict) -> list[dict]:
    """
    Price-Time Priority:
    - BUY matches best ask first
    - SELL matches best bid first
    - Trade price = resting order price
    """
    trades = []
    option_id = incoming["option_id"]

    while _remaining(incoming) > 0:
        if incoming["side"] == BUY:
            book_side = _get_asks(option_id)
            if not book_side:
                break

            _, resting_id = book_side[0]
            resting = _orders[resting_id]

            # bid must be >= ask
            if incoming["price"] < resting["price"]:
                break

        else:
            book_side = _get_bids(option_id)
            if not book_side:
                break

            _, resting_id = book_side[0]
            resting = _orders[resting_id]

            # ask must be <= bid
            if incoming["price"] > resting["price"]:
                break

        fill_qty = min(_remaining(incoming), _remaining(resting))
        trade_price = resting["price"]
        trade_id = "T-" + str(uuid.uuid4())[:8].upper()

        incoming["filled"] += fill_qty
        resting["filled"] += fill_qty

        trades.append({
            "trade_id": trade_id,
            "resting_order_id": resting["order_id"],
            "resting_user_id": resting.get("user_id"),
            "price": trade_price,
            "quantity": fill_qty,
            "resting_filled_quantity": int(resting["filled"]),
            "resting_remaining_quantity": int(_remaining(resting)),
        })

        if _remaining(resting) == 0:
            _remove_from_book(resting)

    return trades


# ─────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────
class ContinuousMatchRequest(BaseModel):
    order_id: str
    user_id: str
    option_id: str
    side: str
    price: float
    remain_quantity: int
    created_date: str


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


# ─────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────
@router.post("/continuous-match", response_model=ContinuousMatchResponse)
def continuous_match(req: ContinuousMatchRequest):
    """
    Continuous matching using in-memory order book.

    IMPORTANT:
    - API only receives ONE incoming order each call.
    - Resting orders must already exist in this API's in-memory book
      from previous calls.
    - If the server restarts, the in-memory book is lost.
    """
    incoming = {
        "order_id": str(req.order_id),
        "user_id": str(req.user_id),
        "option_id": str(req.option_id),
        "side": _normalize_side(req.side),
        "price": float(req.price),
        "quantity": int(req.remain_quantity),
        "filled": 0,
        "submitted_at": _parse_datetime(req.created_date),
    }

    # Save incoming order into registry before matching
    _orders[incoming["order_id"]] = incoming

    trades = _match(incoming)

    # If not fully filled, keep remaining quantity in book
    if _remaining(incoming) > 0:
        _add_to_book(incoming)

    filled = int(incoming["filled"])
    remaining = int(_remaining(incoming))
    requested = int(incoming["quantity"])
    status = _get_order_status(incoming)

    if status == STATUS_FILLED:
        message = "Order fully filled"
    elif status == STATUS_PARTIAL:
        message = f"Partially filled {filled}/{requested}, {remaining} units resting in book"
    else:
        message = "No matching order found, order resting in book"

    updated_orders: list[UpdatedOrder] = []
    for t in trades:
        if t["resting_remaining_quantity"] == 0:
            resting_status = STATUS_FILLED
        elif t["resting_filled_quantity"] > 0:
            resting_status = STATUS_PARTIAL
        else:
            resting_status = STATUS_NEW

        updated_orders.append(
            UpdatedOrder(
                order_id=t["resting_order_id"],
                filled_quantity=t["resting_filled_quantity"],
                remaining_quantity=t["resting_remaining_quantity"],
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
