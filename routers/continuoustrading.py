from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn

from models import (
    OrderRequest, OrderResponse, MatchResult,
    OrderBookSnapshot, TradeHistory, CancelOrderRequest
)
from engine import MatchingEngine

engine = MatchingEngine()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed some ATO residual orders for demo
    engine.seed_ato_residuals()
    yield

app = FastAPI(
    title="Continuous Trading Matching Engine",
    description="Price-Time Priority Matching Engine for Continuous Trading Session",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"message": "Continuous Trading Engine is running", "session": "CT"}


@app.post("/orders", response_model=OrderResponse, summary="Submit a new order")
def submit_order(req: OrderRequest):
    """
    Submit a buy or sell order into the continuous trading session.
    - Orders are matched immediately using price-time priority.
    - Unmatched (or partially matched) orders rest in the order book.
    """
    result = engine.submit_order(req)
    return result


@app.delete("/orders/{order_id}", summary="Cancel an order")
def cancel_order(order_id: str):
    """Cancel a resting order by its ID."""
    success = engine.cancel_order(order_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found or already filled")
    return {"message": f"Order {order_id} cancelled successfully"}


@app.get("/orderbook/{symbol}", response_model=OrderBookSnapshot, summary="Get order book")
def get_orderbook(symbol: str, depth: int = 10):
    """
    Get the current order book snapshot for a symbol.
    Returns top N price levels on each side.
    """
    return engine.get_orderbook(symbol, depth)


@app.get("/trades", response_model=list[TradeHistory], summary="Get trade history")
def get_trades(symbol: str = None, limit: int = 50):
    """Get recent matched trades, optionally filtered by symbol."""
    return engine.get_trades(symbol, limit)


@app.get("/orders/{order_id}", summary="Get order status")
def get_order(order_id: str):
    """Get the current status of a specific order."""
    order = engine.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return order


@app.get("/orders", summary="List all active orders")
def list_orders(symbol: str = None, side: str = None):
    """List all active (unfilled) orders in the order book."""
    return engine.list_orders(symbol, side)


@app.delete("/orderbook/{symbol}/reset", summary="Reset order book (dev only)")
def reset_orderbook(symbol: str):
    """Clear all orders and trades for a symbol."""
    engine.reset(symbol)
    return {"message": f"Order book for {symbol} has been reset"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
