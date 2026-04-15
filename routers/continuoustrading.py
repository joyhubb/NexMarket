from fastapi import APIRouter
from pydantic import BaseModel, Field
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
# In-memory order book
# key = option_id
# value = SortedList of tuples: (sort_key, order_id)
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
    """
    Parse ISO-8601 string from Bubble.
    Falls back to current UTC time if invalid.
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return _utc_now()


def _normalize_side(value: str) -> str:
    """
    Accept BUY / SELL / YES / NO.
    YES is mapped to BUY, NO is mapped to SELL only for matching side semantics.
    Adjust this if your Bubble side field is always BUY/SELL.
    """
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
    """
    Best bid first: higher price first, then earlier time first.
    """
    return (-price, ts.timestamp())


def _ask_key(price: float, ts: datetime):
    """
    Best ask first: lower price first, then earlier time first.
    """
    return (price, ts.timestamp())


def _remaining(order: dict) -> int:
    return max(0, int(order["quantity"]) - int(order["filled"]))


def _add_to_book(order: dict) -> None:
    """
    Add an order to the in-memory order book.
    Assumes the order still has remaining quantity > 0.
    """
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
    """
    Remove an order from the in-memory order book.
    Safe to call even if the tuple is already missing.
    """
    ts = order["submitted_at"]
    oid = order["option_id"]

    try:
        if order["side"] == BUY:
            _get_bids(oid).remove((_bid_key(order["price"], ts), order["order_id"]))
        else:
            _get_asks(oid).remove((_ask_key(order["price"], ts), order["order_id"]))
    except ValueError:
        pass


def _build_internal_order(payload: dict) -> dict:
    """
    Convert Bubble payload order into internal order format.

    Expected payload fields:
    - order_id
    - user_id
    - option_id
    - side
    - price
    - remain_quantity
    - created_date
    """
    remain_quantity = int(payload["remain_quantity"])
    if remain_quantity < 0:
        remain_quantity = 0

    return {
        "order_id": str(payload["order_id"]),
        "user_id": str(payload["user_id"]),
        "option_id": str(payload["option_id"]),
        "side": _normalize_side(payload["side"]),
        "price": float(payload["price"]),
        "quantity": remain_quantity,   # remaining quantity only
        "filled": 0,                   # reset for this matching run
        "submitted_at": _parse_datetime(str(payload["created_date"])),
    }


def _reset_books() -> None:
    _bids.clear()
    _asks.clear()
    _orders.clear()


def _match(incoming: dict) -> list[dict]:
    """
    Match according to Price-Time Priority.

    BUY incoming:
      - matches lowest ask first
      - if same price, earlier resting order first

    SELL incoming:
      - matches highest bid first
      - if same price, earlier resting order first

    Trade price = resting order price
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

            # BUY can match only if incoming bid >= resting ask
            if incoming["price"] < resting["price"]:
                break

        else:
            book_side = _get_bids(option_id)
            if not book_side:
                break

            _, resting_id = book_side[0]
            resting = _orders[resting_id]

            # SELL can match only if incoming ask <= resting bid
            if incoming["price"] > resting["price"]:
                break

        fill_qty = min(_remaining(incoming), _remaining(resting))
        trade_price = resting["price"]
        trade_id = "T-" + str(uuid.uuid4())[:8].upper()

        incoming["filled"] += fill_qty
        resting["filled"] += fill_qty

        resting_remaining = _remaining(resting)
        incoming_remaining = _remaining(incoming)

        trades.append({
            "trade_id": trade_id,
            "resting_order_id": resting["order_id"],
            "resting_user_id": resting.get("user_id"),
            "incoming_order_id": incoming["order_id"],
            "incoming_user_id": incoming.get("user_id"),
            "option_id": option_id,
            "price": trade_price,
            "quantity": fill_qty,
            "resting_filled_quantity": int(resting["filled"]),
            "resting_remaining_quantity": int(resting_remaining),
            "incoming_filled_quantity": int(incoming["filled"]),
            "incoming_remaining_quantity": int(incoming_remaining),
        })

        if resting_remaining == 0:
            _remove_from_book(resting)

    return trades


# ─────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────
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
    side: str
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
    Continuous matching after ATO.

    Bubble sends:
    - incoming_order: the newly placed order
    - order_book: all current resting orders for the same option
                  (typically leftover orders from ATO + earlier CT resting orders)

    This endpoint:
    1. resets in-memory state
    2. loads resting orders into the book
    3. matches the incoming order against the book
    4. returns incoming order result + updated resting orders
    """
    _reset_books()

    # 1) Load resting orders into book
    incoming_order_id = req.incoming_order.order_id
    incoming_option_id = req.incoming_order.option_id

    for o in req.order_book:
        # Skip the incoming order itself if Bubble accidentally includes it
        if o.order_id == incoming_order_id:
            continue

        # Only load same option_id into this matching run
        if o.option_id != incoming_option_id:
            continue

        internal_order = _build_internal_order(o.model_dump())

        if internal_order["quantity"] > 0:
            _add_to_book(internal_order)

    # 2) Build incoming order
    incoming = _build_internal_order(req.incoming_order.model_dump())
    _orders[incoming["order_id"]] = incoming

    # 3) Match
    trades = _match(incoming)

    # 4) If still remaining, keep it resting in book
    if _remaining(incoming) > 0:
        _add_to_book(incoming)

    filled = int(incoming["filled"])
    remaining = int(_remaining(incoming))
    requested = int(incoming["quantity"])

    if remaining == 0:
        status = STATUS_FILLED
        message = "Order fully filled"
    elif filled > 0:
        status = STATUS_PARTIAL
        message = f"Partially filled {filled}/{requested}, {remaining} units resting in book"
    else:
        status = STATUS_NEW
        message = "No matching order found, order resting in book"

    updated_orders: list[UpdatedOrder] = []

    for t in trades:
        resting_filled = int(t["resting_filled_quantity"])
        resting_remaining = int(t["resting_remaining_quantity"])

        if resting_remaining == 0:
            resting_status = STATUS_FILLED
        elif resting_filled > 0:
            resting_status = STATUS_PARTIAL
        else:
            resting_status = STATUS_NEW

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
