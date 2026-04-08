from fastapi import FastAPI
from routers import payment, clearingprice, matching, continuoustrading

app = FastAPI()

@app.get("/")
def root():
    return {"status": "API running"}


app.include_router(payment.router)
app.include_router(clearingprice.router, tags=["ATO"])
app.include_router(matching.router, tags=["Continuous Trading"])
app.include_router(continuoustrading.router, tags=["Continuous Trading"])
