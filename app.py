import json
import os
import time
from contextlib import contextmanager

import oracledb
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Banking AI Anomaly Scorer (Oracle)", version="1.0.0")

# Oracle settings injected via Code Engine secret
ORACLE_DSN = os.getenv("ORACLE_DSN")            # host:port/service
ORACLE_USER = os.getenv("ORACLE_USER", "bank_app")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD")
# pool   = one shared pool per process (default)
# single = new connection on every request (how the Db2 scorer behaves)
POOL_MODE = os.getenv("ORACLE_POOL_MODE", "pool").lower()
POOL_MAX = int(os.getenv("ORACLE_POOL_MAX", "10"))
MODEL_VERSION = "rules-v1"
# BANK_APP owns no tables: resolve unqualified names in the BANK schema
SCHEMA = os.getenv("ORACLE_SCHEMA", "bank")

_pool = None


def err_name(e):
    """Exception class plus the ORA code if any. Never the message, which can echo input."""
    code = getattr(getattr(e, "args", [None])[0], "full_code", "") if getattr(e, "args", None) else ""
    return f"{type(e).__name__} {code}".strip()


@contextmanager
def get_conn():
    """Yield one connection. Pool mode reuses connections, single mode does not."""
    global _pool
    if not (ORACLE_DSN and ORACLE_PASSWORD):
        raise RuntimeError("ORACLE_DSN / ORACLE_PASSWORD not set")
    if POOL_MODE == "single":
        conn = oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD,
                                dsn=ORACLE_DSN, tcp_connect_timeout=10)
        conn.current_schema = SCHEMA
        try:
            yield conn
        finally:
            conn.close()
        return
    if _pool is None:
        _pool = oracledb.create_pool(user=ORACLE_USER, password=ORACLE_PASSWORD,
                                     dsn=ORACLE_DSN, min=1, max=POOL_MAX, increment=1,
                                     tcp_connect_timeout=10)
    conn = _pool.acquire()
    conn.current_schema = SCHEMA
    try:
        yield conn
    finally:
        _pool.release(conn)


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


ACCT_STATUS_SQL = "SELECT status FROM account WHERE acct_id = :1"
RECENT_TXN_SQL = ("SELECT txn_id, amount, country, channel FROM txn WHERE acct_id = :1 "
                  "ORDER BY txn_ts DESC, txn_id DESC FETCH FIRST 20 ROWS ONLY")


def load_and_score(conn, account_id: int):
    """Same reads as the Db2 and Mongo scorers, then the unchanged scoring function."""
    cur = conn.cursor()
    db_start = time.perf_counter()
    cur.execute(ACCT_STATUS_SQL, [account_id])
    row = cur.fetchone()
    is_dormant = bool(row and row[0] and row[0].strip().upper() == "D")
    cur.execute(RECENT_TXN_SQL, [account_id])
    rows = cur.fetchall()
    db_ms = round((time.perf_counter() - db_start) * 1000.0, 2)

    transactions = [{
        "txn_id": int(r[0]),
        "amount": float(r[1]),
        "country": r[2].strip() if r[2] else "N/A",
        "reason": r[3].strip() if r[3] else "N/A",
    } for r in rows]
    if not transactions:
        raise HTTPException(status_code=404,
                            detail=f"No transactions found in Oracle for Account {account_id}")
    latest = transactions[0]
    result = score_transaction_risk(amount=latest["amount"], country=latest["country"],
                                    is_dormant=is_dormant, count_recent=len(transactions))
    return is_dormant, transactions, latest, result, db_ms


def build_response(account_id, is_dormant, transactions, latest, result, db_ms, start_time):
    return {
        "account_id": account_id,
        "is_dormant_account": is_dormant,
        "latest_transaction": latest,
        "total_analyzed_txns": len(transactions),
        "ai_evaluation": result,
        "latency_ms": round((time.perf_counter() - start_time) * 1000.0, 2),
        "db_ms": db_ms,
        "backend": "oracle",
        "pool_mode": POOL_MODE,
    }


@app.get("/health")
def health_check():
    """Readiness probe used by Code Engine router. Does not touch the database."""
    return {"status": "ready", "service": "banking-ai-inference", "backend": "oracle",
            "pool_mode": POOL_MODE}


@app.get("/score-account/{account_id}")
def score_account(account_id: int):
    """Read-only. Same contract as the Db2 and MongoDB scorers."""
    start_time = time.perf_counter()
    try:
        with get_conn() as conn:
            parts = load_and_score(conn, account_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Oracle Error: {err_name(e)}")
        raise HTTPException(status_code=503, detail="Oracle query failed.")
    return build_response(account_id, *parts, start_time)


@app.post("/score-and-store/{account_id}")
def score_and_store(account_id: int):
    """Score the account, then keep the result in AI_RISK_SCORE (idempotent per
    transaction and model version). PATTERNS is a native JSON column."""
    start_time = time.perf_counter()
    try:
        with get_conn() as conn:
            is_dormant, txns, latest, result, db_ms = load_and_score(conn, account_id)
            cur = conn.cursor()
            w0 = time.perf_counter()
            bind = {"a": account_id, "t": latest["txn_id"], "m": MODEL_VERSION,
                    "s": result["risk_score"], "l": result["risk_level"],
                    "f": 1 if result["is_flagged"] else 0,
                    "p": json.dumps(result["detected_patterns"])}
            cur.execute("UPDATE ai_risk_score SET risk_score = :s, risk_level = :l, "
                        "is_flagged = :f, patterns = JSON(:p), scored_at = SYSTIMESTAMP "
                        "WHERE txn_id = :t AND model_version = :m", bind)
            if cur.rowcount == 0:
                cur.execute("INSERT INTO ai_risk_score (acct_id, txn_id, model_version, "
                            "risk_score, risk_level, is_flagged, patterns) "
                            "VALUES (:a, :t, :m, :s, :l, :f, JSON(:p))", bind)
            conn.commit()
            write_ms = round((time.perf_counter() - w0) * 1000.0, 2)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Oracle Error: {err_name(e)}")
        raise HTTPException(status_code=503, detail="Oracle write failed.")
    out = build_response(account_id, is_dormant, txns, latest, result, db_ms, start_time)
    out["stored"] = {"txn_id": latest["txn_id"], "model_version": MODEL_VERSION,
                     "write_ms": write_ms}
    return out


@app.get("/stored-scores/{account_id}")
def stored_scores(account_id: int):
    """Read the stored AI output back, including a query inside the JSON column."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT txn_id, model_version, risk_score, risk_level, is_flagged, "
                        "patterns, scored_at, "
                        "JSON_EXISTS(patterns, '$[*]?(@ == \"SUDDEN_ACTIVITY_ON_DORMANT_ACCOUNT\")') "
                        "FROM ai_risk_score WHERE acct_id = :1 ORDER BY scored_at DESC "
                        "FETCH FIRST 20 ROWS ONLY", [account_id])
            rows = cur.fetchall()
    except Exception as e:
        print(f"Oracle Error: {err_name(e)}")
        raise HTTPException(status_code=503, detail="Oracle query failed.")
    return {"account_id": account_id, "count": len(rows), "scores": [{
        "txn_id": int(r[0]), "model_version": r[1], "risk_score": float(r[2]),
        "risk_level": r[3], "is_flagged": bool(r[4]), "patterns": r[5],
        "scored_at": r[6].isoformat(), "dormant_pattern": bool(r[7]),
    } for r in rows]}


@app.get("/similar/{txn_id}")
def similar(txn_id: int, k: int = 10, approx: bool = False):
    """AI Vector Search: k transactions closest to txn_id (cosine distance)."""
    k = max(1, min(k, 50))
    fetch = "FETCH APPROX FIRST %d ROWS ONLY" % k if approx else "FETCH FIRST %d ROWS ONLY" % k
    sql = ("SELECT f.txn_id, t.acct_id, t.amount, t.country, t.txn_type, "
           "VECTOR_DISTANCE(f.embedding, (SELECT embedding FROM txn_features WHERE txn_id = :1), "
           "COSINE) AS dist FROM txn_features f JOIN txn t ON t.txn_id = f.txn_id "
           "WHERE f.txn_id <> :2 ORDER BY dist " + fetch)
    try:
        t0 = time.perf_counter()
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, [txn_id, txn_id])
            rows = cur.fetchall()
        ms = round((time.perf_counter() - t0) * 1000.0, 2)
    except Exception as e:
        print(f"Oracle Error: {err_name(e)}")
        raise HTTPException(status_code=503, detail="Oracle query failed.")
    return {"txn_id": txn_id, "k": k, "approximate": approx, "query_ms": ms,
            "neighbours": [{"txn_id": int(r[0]), "acct_id": int(r[1]), "amount": float(r[2]),
                            "country": r[3].strip(), "txn_type": r[4].strip(),
                            "distance": round(float(r[5]), 4)} for r in rows]}
