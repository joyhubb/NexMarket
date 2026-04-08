from fastapi import FastAPI
from routers import payment, clearingprice, continuoustrading

app = FastAPI()

@app.get("/")
def root():
    return {"status": "API running"}


app.include_router(payment.router)
app.include_router(clearingprice.router, tags=["ATO"])
app.include_router(continuoustrading.router, tags=["Continuous Trading"])
