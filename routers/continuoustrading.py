from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from sortedcontainers import SortedList
import uuid

router = APIRouter()

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
BUY  = "BUY"
SELL = "SELL"

STATUS_NEW     = "NEW"
STATUS_PARTIAL = "PARTIAL"
STATUS_FILLED  = "FILLED"

# ─────────────────────────────────────────────
# In-memory order book  (per market_id)
# ─────────────────────────────────────────────
_bids: dict   = {}   # market_id → SortedList
_asks: dict   = {}   # market_id → SortedList
_orders: dict = {}   # order_id  → order_dict


def _get_bids(market_id: str) -> SortedList:
    if market_id not in _bids:
        _bids[market_id] = SortedList(key=lambda x: x[0])
    return _bids[market_id]


def _get_asks(market_id: str) -> SortedList:
    if market_id not in _asks:
        _asks[market_id] = SortedList(key=lambda x: x[0])
    return _asks[market_id]


def _bid_key(price: float, ts: datetime):
    return (-price, ts.timestamp())


def _ask_key(price: float, ts: datetime):
    return (price, ts.timestamp())


def _add_to_book(order: dict):
    ts = order["submitted_at"]
    if order["side"] == BUY:
        _get_bids(order["market_id"]).add((_bid_key(order["price"], ts), order["order_id"]))
    else:
        _get_asks(order["market_id"]).add((_ask_key(order["price"], ts), order["order_id"]))
    _orders[order["order_id"]] = order


def _remove_from_book(order: dict):
    ts = order["submitted_at"]
    try:
        if order["side"] == BUY:
            _get_bids(order["market_id"]).remove((_bid_key(order["price"], ts), order["order_id"]))
        else:
            _get_asks(order["market_id"]).remove((_ask_key(order["price"], ts), order["order_id"]))
    except ValueError:
        pass


def _remaining(order: dict) -> int:
    return order["quantity"] - order["filled"]


def _match(incoming: dict) -> list[dict]:
    trades = []
    market_id = incoming["market_id"]

    while _remaining(incoming) > 0:
        if incoming["side"] == BUY:
            book_side = _get_asks(market_id)
            if not book_side:
                break
            _, resting_id = book_side[0]
            resting = _orders[resting_id]
            if incoming["price"] < resting["price"]:
                break
        else:
            book_side = _get_bids(market_id)
            if not book_side:
                break
            _, resting_id = book_side[0]
            resting = _orders[resting_id]
            if incoming["price"] > resting["price"]:
                break

        fill_qty    = min(_remaining(incoming), _remaining(resting))
        trade_price = resting["price"]
        trade_id    = "T-" + str(uuid.uuid4())[:8].upper()

        trades.append({
            "trade_id":         trade_id,
            "resting_order_id": resting["order_id"],
            "resting_user_id":  resting.get("user_id"),
            "price":            trade_price,
            "quantity":         fill_qty,
        })

        incoming["filled"] += fill_qty
        resting["filled"]  += fill_qty

        if _remaining(resting) == 0:
            _remove_from_book(resting)

    return trades


# ─────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────
class ContinuousMatchRequest(BaseModel):
    incoming_order_id:  str
    incoming_user_id:   str
    market_id:          str
    side:               str
    price:              float
    remaining_quantity: int
    created_at:         str


class MatchedTrade(BaseModel):
    trade_id:         str
    resting_order_id: str
    resting_user_id:  Optional[str]
    price:            float
    quantity:         int


class ContinuousMatchResponse(BaseModel):
    incoming_order_id:  str
    market_id:          str
    side:               str
    requested_quantity: int
    filled_quantity:    int
    remaining_quantity: int
    status:             str
    trades:             list[MatchedTrade]
    message:            str


# ─────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────
@router.post("/continuous-match", response_model=ContinuousMatchResponse)
def continuous_match(req: ContinuousMatchRequest):
    try:
        submitted_at = datetime.fromisoformat(req.created_at.replace("Z", "+00:00"))
    except ValueError:
        submitted_at = datetime.utcnow()

    side = BUY if req.side.strip().upper() == BUY else SELL

    order = {
        "order_id":     req.incoming_order_id,
        "user_id":      req.incoming_user_id,
        "market_id":    req.market_id,
        "side":         side,
        "price":        req.price,
        "quantity":     req.remaining_quantity,
        "filled":       0,
        "submitted_at": submitted_at,
    }
    _orders[order["order_id"]] = order

    trades = _match(order)

    if _remaining(order) > 0:
        _add_to_book(order)

    filled    = order["filled"]
    remaining = _remaining(order)

    if remaining == 0:
        status = STATUS_FILLED
        msg    = "Order fully filled"
    elif filled > 0:
        status = STATUS_PARTIAL
        msg    = f"Partially filled {filled}/{req.remaining_quantity}, {remaining} units resting in book"
    else:
        status = STATUS_NEW
        msg    = "No matching order found, order resting in book"

    return ContinuousMatchResponse(
        incoming_order_id  = req.incoming_order_id,
        market_id          = req.market_id,
        side               = side,
        requested_quantity = req.remaining_quantity,
        filled_quantity    = filled,
        remaining_quantity = remaining,
        status             = status,
        trades             = [MatchedTrade(**t) for t in trades],
        message            = msg,
    )
