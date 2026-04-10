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
# In-memory order book (per option_id)
# key = option_id (Bubble's Option Slug)
# ─────────────────────────────────────────────
_bids:   dict = {}   # option_id → SortedList
_asks:   dict = {}   # option_id → SortedList
_orders: dict = {}   # order_id  → order_dict


def _get_bids(option_id: str) -> SortedList:
    if option_id not in _bids:
        _bids[option_id] = SortedList(key=lambda x: x[0])
    return _bids[option_id]


def _get_asks(option_id: str) -> SortedList:
    if option_id not in _asks:
        _asks[option_id] = SortedList(key=lambda x: x[0])
    return _asks[option_id]


def _bid_key(price: float, ts: datetime):
    """Higher price first → earlier time first."""
    return (-price, ts.timestamp())


def _ask_key(price: float, ts: datetime):
    """Lower price first → earlier time first."""
    return (price, ts.timestamp())


def _add_to_book(order: dict):
    ts = order["submitted_at"]
    oid = order["option_id"]
    if order["side"] == BUY:
        _get_bids(oid).add((_bid_key(order["price"], ts), order["order_id"]))
    else:
        _get_asks(oid).add((_ask_key(order["price"], ts), order["order_id"]))
    _orders[order["order_id"]] = order


def _remove_from_book(order: dict):
    ts = order["submitted_at"]
    oid = order["option_id"]
    try:
        if order["side"] == BUY:
            _get_bids(oid).remove((_bid_key(order["price"], ts), order["order_id"]))
        else:
            _get_asks(oid).remove((_ask_key(order["price"], ts), order["order_id"]))
    except ValueError:
        pass


def _remaining(order: dict) -> int:
    return order["quantity"] - order["filled"]


def _match(incoming: dict) -> list[dict]:
    """
    Khớp lệnh theo ưu tiên Giá → Thời gian (Price-Time Priority).
    - BUY:  khớp với ask thấp nhất; nếu cùng giá → ask vào sớm nhất
    - SELL: khớp với bid cao nhất; nếu cùng giá → bid vào sớm nhất
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
            # BUY chỉ khớp nếu bid >= ask
            if incoming["price"] < resting["price"]:
                break
        else:
            book_side = _get_bids(option_id)
            if not book_side:
                break
            _, resting_id = book_side[0]
            resting = _orders[resting_id]
            # SELL chỉ khớp nếu ask <= bid
            if incoming["price"] > resting["price"]:
                break

        fill_qty    = min(_remaining(incoming), _remaining(resting))
        trade_price = resting["price"]   # giá lệnh chờ là giá khớp
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
# Schemas  –  khớp đúng với Bubble data types
# ─────────────────────────────────────────────

class ContinuousMatchRequest(BaseModel):
    # Từ Order record trong Bubble
    order_id:           str           # Unique ID của Order (Bubble slug)
    user_id:            str           # User đặt lệnh
    option_id:          str           # Option slug (thay cho market_id)
    side:               str           # "BUY" hoặc "SELL"
    price:              float         # Order.price
    remain_quantity:    int           # Order.remain quantity
    created_date:       str           # Order.Created Date (ISO-8601)


class MatchedTrade(BaseModel):
    trade_id:           str
    resting_order_id:   str
    resting_user_id:    Optional[str]
    price:              float
    quantity:           int


class ContinuousMatchResponse(BaseModel):
    order_id:           str
    option_id:          str
    side:               str
    requested_quantity: int
    filled_quantity:    int
    remaining_quantity: int
    status:             str           # NEW | PARTIAL | FILLED
    trades:             list[MatchedTrade]
    message:            str


# ─────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────

@router.post("/continuous-match", response_model=ContinuousMatchResponse)
def continuous_match(req: ContinuousMatchRequest):
    """
    Khớp lệnh liên tục (CT) sau phiên ATO.
    Ưu tiên: Giá tốt hơn trước → cùng giá thì lệnh vào sớm hơn (theo giây) trước.
    Lệnh dư ATO đã có sẵn trong sổ với timestamp cũ → tự động ưu tiên hơn lệnh CT mới.
    """
    # Parse timestamp từ Bubble (ISO-8601)
    try:
        submitted_at = datetime.fromisoformat(req.created_date.replace("Z", "+00:00"))
    except ValueError:
        submitted_at = datetime.utcnow()

    # Normalize side: Bubble Option Set thường là "BUY"/"SELL" hoặc "yes"/"no"
    # Dựa vào Option set "Side Order" – cần confirm, tạm thời handle cả hai
    raw_side = req.side.strip().upper()
    if raw_side in ("BUY", "YES"):
        side = BUY
    else:
        side = SELL

    order = {
        "order_id":     req.order_id,
        "user_id":      req.user_id,
        "option_id":    req.option_id,
        "side":         side,
        "price":        req.price,
        "quantity":     req.remain_quantity,
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
        msg    = f"Partially filled {filled}/{req.remain_quantity}, {remaining} units resting in book"
    else:
        status = STATUS_NEW
        msg    = "No matching order found, order resting in book"

    return ContinuousMatchResponse(
        order_id           = req.order_id,
        option_id          = req.option_id,
        side               = side,
        requested_quantity = req.remain_quantity,
        filled_quantity    = filled,
        remaining_quantity = remaining,
        status             = status,
        trades             = [MatchedTrade(**t) for t in trades],
        message            = msg,
    )
