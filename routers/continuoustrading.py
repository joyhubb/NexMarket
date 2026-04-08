from fastapi import APIRouter
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime
from collections import defaultdict
import copy

router = APIRouter()

# =========================
# MODELS
# =========================

Side = Literal["BUY", "SELL"]
Outcome = Literal["YES", "NO"]
OrderStatus = Literal["OPEN", "PARTIAL", "FILLED", "CANCELLED", "REJECTED"]
MatchMode = Literal["DIRECT", "CROSS", "HYBRID"]

class BookOrder(BaseModel):
    id: str
    user_id: str
    option_id: str
    outcome: Outcome
    side: Side
    price: int = Field(ge=1, le=99)
    remain: int = Field(gt=0)
    created_at: datetime
    status: OrderStatus = "OPEN"

class IncomingOrder(BaseModel):
    id: str
    user_id: str
    option_id: str
    outcome: Outcome
    side: Side
    price: int = Field(ge=1, le=99)
    quantity: int = Field(gt=0)
    created_at: datetime

class Trade(BaseModel):
    trade_no: int
    option_id: str
    quantity: int
    type: Literal["direct", "cross"]

    price_yes: int
    price_no: int

    buy_yes_order_id: Optional[str] = None
    buy_no_order_id: Optional[str] = None
    sell_yes_order_id: Optional[str] = None
    sell_no_order_id: Optional[str] = None

    buy_yes_user_id: Optional[str] = None
    buy_no_user_id: Optional[str] = None
    sell_yes_user_id: Optional[str] = None
    sell_no_user_id: Optional[str] = None

    incoming_order_id: str
    resting_order_id: str
    executed_at: datetime

class OrderUpdate(BaseModel):
    order_id: str
    new_status: Literal["OPEN", "PARTIAL", "FILLED"]
    matched_quantity: int
    remaining_quantity: int

class UserSettlement(BaseModel):
    user_id: str
    cash_spent: int = 0
    cash_received: int = 0
    refund: int = 0

class ContinuousMatchRequest(BaseModel):
    incoming_order: IncomingOrder
    open_orders: List[BookOrder]
    match_mode: MatchMode = "HYBRID"

class ContinuousMatchResponse(BaseModel):
    success: bool
    match_mode: MatchMode

    incoming_order_status: Literal["OPEN", "PARTIAL", "FILLED"]
    incoming_matched_quantity: int
    incoming_remaining_quantity: int

    trades: List[Trade]
    updated_resting_orders: List[OrderUpdate]
    incoming_order_update: OrderUpdate
    settlements: List[UserSettlement]

    last_traded_price_yes: Optional[int]
    last_traded_price_no: Optional[int]

# =========================
# HELPERS
# =========================

def complement_outcome(outcome: Outcome) -> Outcome:
    return "NO" if outcome == "YES" else "YES"

def build_order_update(order_id: str, original_qty: int, remaining_qty: int) -> OrderUpdate:
    matched_qty = original_qty - remaining_qty
    if remaining_qty == 0:
        status = "FILLED"
    elif matched_qty > 0:
        status = "PARTIAL"
    else:
        status = "OPEN"

    return OrderUpdate(
        order_id=order_id,
        new_status=status,
        matched_quantity=matched_qty,
        remaining_quantity=remaining_qty
    )

# =========================
# DIRECT MATCH
# =========================

def is_direct_candidate(incoming: IncomingOrder, resting: BookOrder) -> bool:
    return (
        incoming.option_id == resting.option_id
        and incoming.outcome == resting.outcome
        and incoming.side != resting.side
        and resting.status in ("OPEN", "PARTIAL")
        and resting.remain > 0
    )

def direct_price_cross(incoming: IncomingOrder, resting: BookOrder) -> bool:
    if incoming.side == "BUY":
        return incoming.price >= resting.price
    return incoming.price <= resting.price

def sort_direct_candidates(incoming: IncomingOrder, orders: List[BookOrder]) -> List[BookOrder]:
    if incoming.side == "BUY":
        return sorted(orders, key=lambda o: (o.price, o.created_at))
    return sorted(orders, key=lambda o: (-o.price, o.created_at))

# =========================
# CROSS MATCH
# =========================

def is_cross_candidate(incoming: IncomingOrder, resting: BookOrder) -> bool:
    """
    Cross complement:
    BUY YES  <-> BUY NO
    BUY NO   <-> BUY YES

    Ở đây chỉ support kiểu cross giữa 2 buyer bổ sung nhau.
    """
    return (
        incoming.option_id == resting.option_id
        and incoming.outcome != resting.outcome
        and incoming.side == "BUY"
        and resting.side == "BUY"
        and resting.status in ("OPEN", "PARTIAL")
        and resting.remain > 0
    )

def cross_price_cross(incoming: IncomingOrder, resting: BookOrder) -> bool:
    """
    incoming.price + resting.price >= 100
    """
    return incoming.price + resting.price >= 100

def sort_cross_candidates(incoming: IncomingOrder, orders: List[BookOrder]) -> List[BookOrder]:
    """
    Ưu tiên resting order có giá cao hơn trước, rồi time.
    Vì giá càng cao thì càng dễ tạo cặp >=100.
    """
    return sorted(orders, key=lambda o: (-o.price, o.created_at))

# =========================
# MATCH EXECUTION
# =========================

def apply_direct_match(
    incoming: IncomingOrder,
    book: List[BookOrder],
    start_trade_no: int,
    original_map: dict[str, int]
):
    trades = []
    updates = []
    remaining = incoming.quantity
    trade_no = start_trade_no

    candidates = [
        o for o in book
        if is_direct_candidate(incoming, o) and direct_price_cross(incoming, o)
    ]
    candidates = sort_direct_candidates(incoming, candidates)

    for resting in candidates:
        if remaining == 0:
            break

        qty = min(remaining, resting.remain)
        trade_price = resting.price

        trade = Trade(
            trade_no=trade_no,
            option_id=incoming.option_id,
            quantity=qty,
            type="direct",
            price_yes=trade_price if incoming.outcome == "YES" else 100 - trade_price,
            price_no=trade_price if incoming.outcome == "NO" else 100 - trade_price,
            incoming_order_id=incoming.id,
            resting_order_id=resting.id,
            executed_at=datetime.utcnow()
        )

        if incoming.outcome == "YES":
            if incoming.side == "BUY":
                trade.buy_yes_order_id = incoming.id
                trade.buy_yes_user_id = incoming.user_id
                trade.sell_yes_order_id = resting.id
                trade.sell_yes_user_id = resting.user_id
            else:
                trade.buy_yes_order_id = resting.id
                trade.buy_yes_user_id = resting.user_id
                trade.sell_yes_order_id = incoming.id
                trade.sell_yes_user_id = incoming.user_id
        else:
            if incoming.side == "BUY":
                trade.buy_no_order_id = incoming.id
                trade.buy_no_user_id = incoming.user_id
                trade.sell_no_order_id = resting.id
                trade.sell_no_user_id = resting.user_id
            else:
                trade.buy_no_order_id = resting.id
                trade.buy_no_user_id = resting.user_id
                trade.sell_no_order_id = incoming.id
                trade.sell_no_user_id = incoming.user_id

        trades.append(trade)

        remaining -= qty
        resting.remain -= qty
        trade_no += 1

        updates.append(
            build_order_update(
                order_id=resting.id,
                original_qty=original_map[resting.id],
                remaining_qty=resting.remain
            )
        )

    return trades, updates, remaining, trade_no

def apply_cross_match(
    incoming: IncomingOrder,
    book: List[BookOrder],
    start_trade_no: int,
    original_map: dict[str, int]
):
    trades = []
    updates = []
    remaining = incoming.quantity
    trade_no = start_trade_no

    if incoming.side != "BUY":
        return trades, updates, remaining, trade_no

    candidates = [
        o for o in book
        if is_cross_candidate(incoming, o) and cross_price_cross(incoming, o)
    ]
    candidates = sort_cross_candidates(incoming, candidates)

    for resting in candidates:
        if remaining == 0:
            break

        qty = min(remaining, resting.remain)

        # Giữ giá incoming cho outcome của incoming
        # và outcome bổ sung = 100 - giá incoming
        if incoming.outcome == "YES":
            price_yes = incoming.price
            price_no = 100 - incoming.price
        else:
            price_no = incoming.price
            price_yes = 100 - incoming.price

        trade = Trade(
            trade_no=trade_no,
            option_id=incoming.option_id,
            quantity=qty,
            type="cross",
            price_yes=price_yes,
            price_no=price_no,
            incoming_order_id=incoming.id,
            resting_order_id=resting.id,
            executed_at=datetime.utcnow()
        )

        if incoming.outcome == "YES":
            trade.buy_yes_order_id = incoming.id
            trade.buy_yes_user_id = incoming.user_id
            trade.buy_no_order_id = resting.id
            trade.buy_no_user_id = resting.user_id
        else:
            trade.buy_no_order_id = incoming.id
            trade.buy_no_user_id = incoming.user_id
            trade.buy_yes_order_id = resting.id
            trade.buy_yes_user_id = resting.user_id

        trades.append(trade)

        remaining -= qty
        resting.remain -= qty
        trade_no += 1

        updates.append(
            build_order_update(
                order_id=resting.id,
                original_qty=original_map[resting.id],
                remaining_qty=resting.remain
            )
        )

    return trades, updates, remaining, trade_no

# =========================
# SETTLEMENT
# =========================

def calculate_settlements(incoming: IncomingOrder, trades: List[Trade]) -> List[UserSettlement]:
    settlements = defaultdict(lambda: {"cash_spent": 0, "cash_received": 0, "refund": 0})

    for t in trades:
        if t.type == "direct":
            if t.buy_yes_user_id:
                settlements[t.buy_yes_user_id]["cash_spent"] += t.price_yes * t.quantity
            if t.sell_yes_user_id:
                settlements[t.sell_yes_user_id]["cash_received"] += t.price_yes * t.quantity
            if t.buy_no_user_id:
                settlements[t.buy_no_user_id]["cash_spent"] += t.price_no * t.quantity
            if t.sell_no_user_id:
                settlements[t.sell_no_user_id]["cash_received"] += t.price_no * t.quantity

        elif t.type == "cross":
            if t.buy_yes_user_id:
                settlements[t.buy_yes_user_id]["cash_spent"] += t.price_yes * t.quantity
            if t.buy_no_user_id:
                settlements[t.buy_no_user_id]["cash_spent"] += t.price_no * t.quantity

    return [
        UserSettlement(
            user_id=uid,
            cash_spent=data["cash_spent"],
            cash_received=data["cash_received"],
            refund=data["refund"]
        )
        for uid, data in settlements.items()
    ]

# =========================
# ENGINE
# =========================

@router.post("/continuous-match", response_model=ContinuousMatchResponse)
def continuous_match(data: ContinuousMatchRequest):
    incoming = copy.deepcopy(data.incoming_order)
    book = copy.deepcopy(data.open_orders)

    original_map = {o.id: o.remain for o in data.open_orders}
    all_trades = []
    all_updates = []
    trade_no = 1
    remaining = incoming.quantity

    # DIRECT
    if data.match_mode in ("DIRECT", "HYBRID"):
        temp_incoming = copy.deepcopy(incoming)
        temp_incoming.quantity = remaining

        trades, updates, remaining, trade_no = apply_direct_match(
            temp_incoming, book, trade_no, original_map
        )
        all_trades.extend(trades)
        all_updates.extend(updates)

    # CROSS
    if remaining > 0 and data.match_mode in ("CROSS", "HYBRID"):
        temp_incoming = copy.deepcopy(incoming)
        temp_incoming.quantity = remaining

        trades, updates, remaining, trade_no = apply_cross_match(
            temp_incoming, book, trade_no, original_map
        )
        all_trades.extend(trades)
        all_updates.extend(updates)

    dedup = {}
    for u in all_updates:
        dedup[u.order_id] = u
    all_updates = list(dedup.values())

    incoming_update = build_order_update(
        order_id=incoming.id,
        original_qty=incoming.quantity,
        remaining_qty=remaining
    )

    settlements = calculate_settlements(incoming, all_trades)

    last_traded_price_yes = all_trades[-1].price_yes if all_trades else None
    last_traded_price_no = all_trades[-1].price_no if all_trades else None

    return ContinuousMatchResponse(
        success=True,
        match_mode=data.match_mode,
        incoming_order_status=incoming_update.new_status,
        incoming_matched_quantity=incoming_update.matched_quantity,
        incoming_remaining_quantity=incoming_update.remaining_quantity,
        trades=all_trades,
        updated_resting_orders=all_updates,
        incoming_order_update=incoming_update,
        settlements=settlements,
        last_traded_price_yes=last_traded_price_yes,
        last_traded_price_no=last_traded_price_no
    )
