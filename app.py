import os
import time
from fastapi import FastAPI, HTTPException
import ibm_db

app = FastAPI(title="Banking AI Anomaly Scorer", version="1.0.0")

# Db2 Connection String injected via Code Engine Environment Variable
DB2_CONN_STRING = os.getenv("DB2_CONNECTION_STRING")

def get_db_connection():
    if not DB2_CONN_STRING:
        return None
    try:
        return ibm_db.connect(DB2_CONN_STRING, "", "")
    except Exception as e:
        print(f"Db2 Connection Error: {e}")
        return None

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
    return {"status": "ready", "service": "banking-ai-inference"}

@app.get("/score-account/{account_id}")
def score_account(account_id: int):
    start_time = time.perf_counter()
    conn = get_db_connection()

    if not conn:
        raise HTTPException(status_code=500, detail="Db2 connection failed. Check DB2_CONNECTION_STRING.")

    # 1. Query Account Master Status (Dormant vs Active) from STD21189.ACCOUNT
    acct_sql = "SELECT STATUS, BALANCE FROM STD21189.ACCOUNT WHERE ACCT_ID = ?"
    acct_stmt = ibm_db.prepare(conn, acct_sql)
    ibm_db.execute(acct_stmt, (account_id,))
    acct_row = ibm_db.fetch_assoc(acct_stmt)
    is_dormant = (acct_row["STATUS"].strip().upper() == "D") if acct_row and acct_row["STATUS"] else False

    # 2. Query Recent Transactions from Ledger table STD21189.TXN
    sql = """
        SELECT TXN_ID, ACCT_ID, VARCHAR(TXN_TS) AS TXN_TS, AMOUNT, TXN_TYPE, COUNTRY, CHANNEL 
        FROM STD21189.TXN 
        WHERE ACCT_ID = ? 
        ORDER BY TXN_TS DESC 
        FETCH FIRST 20 ROWS ONLY
    """
    stmt = ibm_db.prepare(conn, sql)
    ibm_db.execute(stmt, (account_id,))

    transactions = []
    row = ibm_db.fetch_assoc(stmt)
    while row:
        transactions.append({
            "txn_id": row["TXN_ID"],
            "amount": float(row["AMOUNT"]),
            "country": row["COUNTRY"].strip() if row["COUNTRY"] else "N/A",
            "reason": row["CHANNEL"].strip() if row["CHANNEL"] else "N/A"
        })
        row = ibm_db.fetch_assoc(stmt)

    ibm_db.close(conn)

    if not transactions:
        raise HTTPException(status_code=404, detail=f"No transactions found in Db2 for Account {account_id}")

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
        "latency_ms": latency_ms
    }
