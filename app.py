import os
import tempfile
import time
from fastapi import FastAPI, HTTPException
from pymongo import MongoClient

app = FastAPI(title="Banking AI Anomaly Scorer (MongoDB)", version="1.0.0")

# MongoDB settings injected via Code Engine secret
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "bankdemo")
MONGO_CA_PEM = os.getenv("MONGO_CA_PEM")          # PEM text of the CA
MONGO_MAX_POOL = int(os.getenv("MONGO_MAX_POOL", "10"))

_client = None


def get_db():
    """One pooled MongoClient per process, created on first use.
    (The Db2 version opens a new connection on every request.)"""
    global _client
    if not MONGO_URI:
        return None
    try:
        if _client is None:
            kw = {"maxPoolSize": MONGO_MAX_POOL, "serverSelectionTimeoutMS": 5000,
                  "connectTimeoutMS": 5000, "socketTimeoutMS": 15000}
            if MONGO_CA_PEM:
                f = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
                f.write(MONGO_CA_PEM)
                f.close()
                kw["tlsCAFile"] = f.name
            _client = MongoClient(MONGO_URI, **kw)
        return _client[MONGO_DB]
    except Exception as e:
        print(f"MongoDB Connection Error: {type(e).__name__}")
        return None


# ---- scoring: copied unchanged from the Db2 version --------------------------
def score_transaction_risk(amount: float, country: str, is_dormant: bool, count_recent: int = 1) -> dict:
    """
    AI Anomaly Scoring Engine:
    - High-value Foreign Anomaly (e.g., RU, large foreign outflow)
    - Anti-Money Laundering Structuring (transactions just under reporting threshold ~9,900)
    - Dormancy Reactivation (DORMANT_ACTIVE)
    - Burst Velocity Anomalies (rapid repeated transactions)
    """
    base_score = 0.05
    detected_patterns = []

    # 1. Jurisdictional Risk Heuristic
    high_risk_countries = {"RU", "IR", "KP", "SY"}
    if country.upper() in high_risk_countries:
        base_score += 0.50
        detected_patterns.append("HIGH_RISK_FOREIGN_JURISDICTION")
    elif country.upper() != "GB":
        base_score += 0.15
        detected_patterns.append("INTERNATIONAL_TRANSACTION")

    # 2. Structuring / Smurfing (Threshold 9,000 - 9,999)
    if 9000 <= amount <= 9999:
        base_score += 0.45
        detected_patterns.append("POSSIBLE_STRUCTURING_UNDER_REPORTING_LIMIT")
    elif amount > 25000:
        base_score += 0.35
        detected_patterns.append("LARGE_VALUE_OUTFLOW")

    # 3. Dormancy Reactivation Heuristic (DORMANT_ACTIVE)
    if is_dormant:
        base_score += 0.45
        detected_patterns.append("SUDDEN_ACTIVITY_ON_DORMANT_ACCOUNT")

    # 4. Burst Frequency / Velocity
    if count_recent > 3:
        velocity_penalty = min(0.30, count_recent * 0.05)
        base_score += velocity_penalty
        detected_patterns.append(f"TRANSACTION_BURST_VELOCITY_{count_recent}_RECENT_TXNS")

    final_score = min(0.99, round(base_score, 2))
    risk_level = "CRITICAL" if final_score >= 0.70 else "MEDIUM" if final_score >= 0.40 else "LOW"

    return {
        "risk_score": final_score,
        "risk_level": risk_level,
        "is_flagged": final_score >= 0.70,
        "detected_patterns": detected_patterns or ["NORMAL_BEHAVIOR"]
    }


@app.get("/health")
def health_check():
    """Readiness probe used by Code Engine router."""
    return {"status": "ready", "service": "banking-ai-inference", "backend": "mongodb"}


@app.get("/score-account/{account_id}")
def score_account(account_id: int):
    start_time = time.perf_counter()
    db = get_db()

    if db is None:
        raise HTTPException(status_code=500, detail="MongoDB connection failed. Check MONGO_URI.")

    try:
        db_start = time.perf_counter()
        # 1. Account master status (Dormant vs Active)
        acct_row = db.accounts.find_one({"_id": account_id}, {"status": 1})
        is_dormant = (acct_row["status"].strip().upper() == "D") if acct_row and acct_row.get("status") else False

        # 2. Recent transactions, newest first, same 20-row cap as the Db2 query
        rows = list(db.transactions.find({"acct_id": account_id})
                    .sort([("txn_ts", -1), ("_id", -1)]).limit(20))
        db_ms = round((time.perf_counter() - db_start) * 1000.0, 2)
    except Exception as e:
        print(f"MongoDB Query Error: {type(e).__name__}")
        raise HTTPException(status_code=503, detail="MongoDB query failed.")

    transactions = [{
        "txn_id": r["_id"],
        "amount": float(r["amount"].to_decimal()),
        "country": r["country"].strip() if r.get("country") else "N/A",
        "reason": r["channel"].strip() if r.get("channel") else "N/A",
    } for r in rows]

    if not transactions:
        raise HTTPException(status_code=404, detail=f"No transactions found in MongoDB for Account {account_id}")

    total_txns = len(transactions)
    latest_txn = transactions[0]
    inference_result = score_transaction_risk(
        amount=latest_txn["amount"],
        country=latest_txn["country"],
        is_dormant=is_dormant,
        count_recent=total_txns
    )

    latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

    return {
        "account_id": account_id,
        "is_dormant_account": is_dormant,
        "latest_transaction": latest_txn,
        "total_analyzed_txns": total_txns,
        "ai_evaluation": inference_result,
        "latency_ms": latency_ms,
        "db_ms": db_ms,
        "backend": "mongodb"
    }
