import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

from database import db, create_document, get_documents
from schemas import TradingTip

app = FastAPI(title="Crypto Trading Tips API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

COINGECKO_API = "https://api.coingecko.com/api/v3"

class GenerateTipRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"  # 15m,1h,4h,1d

@app.get("/")
def read_root():
    return {"message": "Crypto Trading Tips Backend is running"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = getattr(db, 'name', None) or "Unknown"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    return response

@app.get("/schema")
def get_schema():
    return {
        "collections": [
            {
                "name": "tradingtip",
                "schema": TradingTip.model_json_schema(),
            }
        ]
    }

# Utility to fetch current price and a small OHLC sample from CoinGecko

def fetch_market_data(symbol: str):
    # CoinGecko uses ids like 'bitcoin', 'ethereum'; try mapping common tickers
    mapping = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "BNB": "binancecoin",
        "SOL": "solana",
        "ADA": "cardano",
        "XRP": "ripple",
        "DOGE": "dogecoin",
        "MATIC": "polygon-pos",
        "DOT": "polkadot",
        "LTC": "litecoin",
    }
    coin_id = mapping.get(symbol.upper())
    if not coin_id:
        raise HTTPException(status_code=400, detail="Unsupported symbol. Try BTC, ETH, SOL, ADA, XRP, DOGE, MATIC, DOT, LTC, BNB")

    # Price
    price_resp = requests.get(f"{COINGECKO_API}/simple/price", params={"ids": coin_id, "vs_currencies": "usd"}, timeout=10)
    price_resp.raise_for_status()
    price = price_resp.json().get(coin_id, {}).get("usd")

    # Market chart (last 1 day hourly)
    chart_resp = requests.get(f"{COINGECKO_API}/coins/{coin_id}/market_chart", params={"vs_currency": "usd", "days": 1}, timeout=10)
    chart_resp.raise_for_status()
    prices = [p[1] for p in chart_resp.json().get("prices", [])]
    return price, prices

# Simple indicators

def calc_sma(values, period: int) -> Optional[float]:
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def calc_rsi(values, period: int = 14) -> Optional[float]:
    if not values or len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[-i] - values[-i - 1]
        if change >= 0:
            gains.append(change)
        else:
            losses.append(abs(change))
    avg_gain = (sum(gains) / period) if gains else 0.000001
    avg_loss = (sum(losses) / period) if losses else 0.000001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def generate_signal(price: float, prices: List[float]) -> dict:
    sma20 = calc_sma(prices, 20)
    sma50 = calc_sma(prices, 50)
    rsi = calc_rsi(prices, 14)

    signal = "neutral"
    confidence = 0.5
    notes = []

    if sma20 and sma50:
        if sma20 > sma50:
            signal = "buy"
            confidence += 0.2
            notes.append("Short-term trend above long-term (SMA20>SMA50)")
        elif sma20 < sma50:
            signal = "sell"
            confidence += 0.2
            notes.append("Short-term trend below long-term (SMA20<SMA50)")

    if rsi is not None:
        if rsi < 30:
            signal = "buy"
            confidence += 0.2
            notes.append("RSI indicates oversold (<30)")
        elif rsi > 70:
            signal = "sell"
            confidence += 0.2
            notes.append("RSI indicates overbought (>70)")
        else:
            notes.append("RSI in neutral range")

    confidence = min(max(confidence, 0.0), 1.0)

    indicators = {"sma20": round(sma20, 4) if sma20 else None,
                  "sma50": round(sma50, 4) if sma50 else None,
                  "rsi": round(rsi, 2) if rsi is not None else None}

    tip_text = f"Sinal: {signal.upper()} | Confiança: {int(confidence*100)}%. " + " ".join(notes)

    return {
        "signal": signal,
        "confidence": confidence,
        "indicators": indicators,
        "tip": tip_text,
        "notes": "; ".join(notes) if notes else None,
    }

@app.post("/api/generate_tip")
def api_generate_tip(payload: GenerateTipRequest):
    price, prices = fetch_market_data(payload.symbol)
    analysis = generate_signal(price, prices)

    doc = TradingTip(
        symbol=payload.symbol.upper(),
        timeframe=payload.timeframe,
        price=price,
        indicators=analysis["indicators"],
        signal=analysis["signal"],
        confidence=analysis["confidence"],
        tip=analysis["tip"],
        notes=analysis.get("notes")
    )

    try:
        inserted_id = create_document("tradingtip", doc)
    except Exception:
        inserted_id = None

    return {"id": inserted_id, **doc.model_dump()}

@app.get("/api/tips")
def list_tips(symbol: Optional[str] = Query(None)):
    filt = {"symbol": symbol.upper()} if symbol else {}
    try:
        docs = get_documents("tradingtip", filt, limit=50)
        # Convert ObjectId and datetime for JSON
        for d in docs:
            if "_id" in d:
                d["_id"] = str(d["_id"])
            for k in ["created_at", "updated_at"]:
                if k in d and hasattr(d[k], 'isoformat'):
                    d[k] = d[k].isoformat()
        return {"items": docs}
    except Exception:
        # If DB not available, return empty list gracefully
        return {"items": []}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
