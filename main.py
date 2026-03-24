from fastapi import FastAPI
from routers import payment, clearingprice

app = FastAPI()

@app.get("/")
def root():
    return {"status": "API running"}


app.include_router(payment.router)
app.include_router(clearingprice.router, tags=["ATO"])