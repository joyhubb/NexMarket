from fastapi import APIRouter
from typing import List, Optional, Dict
from pydantic import BaseModel, Field
from datetime import datetime
from collections import defaultdict
import copy

router = APIRouter()

MATCHING_TYPE_CROSS = "cross"
MATCHING_TYPE_sAME = "same side"

SIDE_YES = "yes"
SIDE_NO = "no"

# =========================
# MODELS
# =========================
class Order(BaseModel):
    id: str
    user_id: str
    price: int = Field(ge=1, le=99)
    remain: int = Field(gt=0)
    created_at: datetime

class Trade(BaseModel):
    type: str 

    user_id_a: str 
    user_id_b: str 

    order_id_a: str 
    order_id_b: str    

    outcome: Optional[str] = None 

    price: int
    quantity: int

class MatchingRequest(BaseModel):
    option_id: str

    orders_buy_yes: List[Order]
    orders_sell_yes: List[Order]

    orders_buy_no: List[Order]
    orders_sell_no: List[Order]

class UserUpdate(BaseModel):
    user_id: str
    spent: int = 0
    received: int = 0
    yes_delta: int = 0
    no_delta: int = 0

# =========================
# SORT
# =========================
def sort_buy(orders: List[Order]):
    orders.sort(key=lambda o: (-o.price, o.created_at))

def sort_sell(orders: List[Order]):
    orders.sort(key=lambda o: (o.price, o.created_at))

# =========================
# CROSS MATCHING
# =========================
def match_cross(buy_yes: List[Order], buy_no: List[Order]) -> List[Trade]:
    trades = []

    sort_buy(buy_yes)
    no_buckets: Dict[int, List[Order]] = defaultdict(list)
    for o in buy_no:
        no_buckets[o.price].append(o)
    for price in no_buckets:
        no_buckets[price].sort(key=lambda o: o.created_at)
    
    for y in buy_yes:
        if y.remain == 0:
            continue

        target_price = 100 - y.price
        if target_price not in no_buckets:
            continue
        no_list = no_buckets [target_price]
        j = 0
        while j < len(no_list) and y.remain > 0:
            n = no_list[j]
            if n.remain == 0:
                j += 1
                continue
            qty = min(y.remain, n.remain)
            trades.append(Trade(
                type=MATCHING_TYPE_CROSS,
                user_id_a=y.user_id,    # buyer YES
                user_id_b=n.user_id,    # buyer NO
                order_id_a=y.id,
                order_id_b=n.id,
                outcome=None,
                price=y.price,
                quantity=qty
            ))

            y.remain -= qty
            n.remain -= qty

            if n.remain == 0:
                j += 1
    
    return trades

# =========================
# SAME SIDE MATCHING
# =========================
def match_same_side(
    buy_orders: List[Order],
    sell_orders: List[Order],
    outcome: str
) -> List[Trade]:
    trades = []
    sort_buy(buy_orders)
    sort_sell(sell_orders)

    i, j = 0, 0
    while i < len(buy_orders) and i < len(sell_orders):
        b = buy_orders[i]
        s = sell_orders[j]

        if b.price < s.price:
            break
        qty = min(b.remain, s.remain)

        trades.append(Trade(
            type=MATCHING_TYPE_sAME,
            outcome=outcome,
            user_id_a=b.user_id,    # buyer
            user_id_b=s.user_id,    # seller
            order_id_a=b.id,
            order_id_b=s.id,
            price=s.price,
            quantity=qty
        ))