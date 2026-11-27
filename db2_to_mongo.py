#!/usr/bin/env python3
"""
db2_to_mongo.py - copy BANKDEMO from Db2 on IBM Cloud into MongoDB (RHEL)

  Db2 on Cloud (ibm_db, TLS)  --read-->  this script  --TLS write-->  MongoDB

DOCUMENT MODEL (shaped for the two API questions, not a 1:1 table copy)
  accounts      _id = ACCT_ID, customer embedded (one read answers "get account")
  transactions  _id = TXN_ID, indexes on (txn_ts) and (acct_id, txn_ts)
  sync_status   one document per run, same idea as BANKDEMO.SYNC_STATUS

TYPES
  money      -> Decimal128 (exact, comparable in queries; never float)
  DATE       -> UTC datetime at midnight
  TIMESTAMP  -> UTC datetime (Db2 value is treated as UTC, as the API does)

BEHAVIOUR
  accounts     full refresh, upserted by key
  transactions incremental: rows at or after (newest txn_ts in Mongo - 1 day),
               upserted by _id. Re-reading is harmless (idempotent).
  --full       ignore the watermark and resend all transactions

USAGE
  python db2_to_mongo.py --check     test both connections, change nothing
  python db2_to_mongo.py             run one sync
  python db2_to_mongo.py --full

CONFIG (environment or .env next to this file, see .env.example)
  CLOUD_HOST CLOUD_PORT CLOUD_DB CLOUD_USER CLOUD_PASSWORD   Db2 source
  MONGO_URI MONGO_DB [MONGO_CA_FILE]                         MongoDB target
  Passwords are never printed.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

BATCH = 1000
OVERLAP = timedelta(days=1)


def load_env(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def need(names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit("Missing settings: " + ", ".join(missing))


def scrub(text):
    for name in ("CLOUD_PASSWORD", "MONGO_URI"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "***")
    return text


def connect_db2():
    import ibm_db_dbi
    dsn = ("DATABASE={db};HOSTNAME={h};PORT={p};PROTOCOL=TCPIP;UID={u};PWD={w};"
           "SECURITY=SSL;CONNECTTIMEOUT=20;").format(
        db=os.environ.get("CLOUD_DB", "BLUDB"), h=os.environ["CLOUD_HOST"],
        p=os.environ["CLOUD_PORT"], u=os.environ["CLOUD_USER"],
        w=os.environ["CLOUD_PASSWORD"])
    return ibm_db_dbi.connect(dsn, "", "")


def connect_mongo():
    from pymongo import MongoClient
    kw = {"serverSelectionTimeoutMS": 8000}
    if os.environ.get("MONGO_CA_FILE"):
        kw["tlsCAFile"] = os.environ["MONGO_CA_FILE"]
    client = MongoClient(os.environ["MONGO_URI"], **kw)
    client.admin.command("ping")                 # proves auth, not just socket
    return client, client[os.environ["MONGO_DB"]]


def d128(v):
    from bson.decimal128 import Decimal128
    return Decimal128(Decimal(str(v)).quantize(Decimal("0.01")))


def utc(v):
    """date or naive datetime from Db2 -> naive UTC datetime (BSON friendly)."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return datetime.fromisoformat(str(v))


def ensure_indexes(db):
    db.transactions.create_index("txn_ts")
    db.transactions.create_index([("acct_id", 1), ("txn_ts", 1)])
    db.accounts.create_index("status")


def bulk_upsert(coll, docs):
    from pymongo import ReplaceOne
    if docs:
        coll.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs],
                        ordered=False)


def sync_accounts(src, db):
    cur = src.cursor()
    cur.execute("SELECT A.ACCT_ID, A.ACCT_TYPE, A.STATUS, A.BALANCE, A.OPENED_DATE, "
                "C.CUST_ID, C.FULL_NAME, C.CITY, C.COUNTRY, C.RISK_BAND "
                "FROM ACCOUNT A JOIN CUSTOMER C ON C.CUST_ID = A.CUST_ID "
                "ORDER BY A.ACCT_ID")
    sent = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        bulk_upsert(db.accounts, [{
            "_id": int(r[0]), "acct_type": r[1].strip(), "status": r[2].strip(),
            "balance": d128(r[3]), "opened_date": utc(r[4]),
            "customer": {"cust_id": int(r[5]), "full_name": r[6], "city": r[7],
                         "country": r[8].strip(), "risk_band": r[9].strip()},
        } for r in rows])
        sent += len(rows)
    return sent


def sync_transactions(src, db, full):
    where, args = "", ()
    if not full:
        newest = db.transactions.find_one(sort=[("txn_ts", -1)], projection=["txn_ts"])
        if newest:
            where = " WHERE TXN_TS >= ?"
            args = ((newest["txn_ts"] - OVERLAP),)
    cur = src.cursor()
    cur.execute("SELECT TXN_ID, ACCT_ID, TXN_TS, AMOUNT, TXN_TYPE, COUNTRY, CHANNEL "
                "FROM TXN" + where + " ORDER BY TXN_ID", args)
    sent = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        bulk_upsert(db.transactions, [{
            "_id": int(r[0]), "acct_id": int(r[1]), "txn_ts": utc(r[2]),
            "amount": d128(r[3]), "txn_type": r[4].strip(),
            "country": r[5].strip(), "channel": r[6].strip(),
        } for r in rows])
        sent += len(rows)
    return sent


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--check", action="store_true", help="test connections only")
    ap.add_argument("--full", action="store_true", help="resend all transactions")
    ap.add_argument("--env", default=os.path.join(os.path.dirname(__file__), ".env"))
    args = ap.parse_args()

    load_env(args.env)
    need(["CLOUD_HOST", "CLOUD_PORT", "CLOUD_USER", "CLOUD_PASSWORD",
          "MONGO_URI", "MONGO_DB"])

    try:
        src = connect_db2()
        print("Db2 cloud: connected")
    except Exception as exc:
        sys.exit("Db2 connection failed: " + scrub(str(exc))[:300])
    try:
        client, db = connect_mongo()
        print("MongoDB  : connected")
    except Exception as exc:
        sys.exit("MongoDB connection failed: " + scrub(str(exc))[:300])

    if args.check:
        print("Check passed. Nothing was changed.")
        return

    ensure_indexes(db)
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    result = {"started": started, "full": args.full}
    try:
        result["accounts"] = sync_accounts(src, db)
        result["transactions"] = sync_transactions(src, db, args.full)
        result["status"] = "OK"
    except Exception as exc:
        result.update(status="FAIL", error=scrub(str(exc))[:500])
    result["ended"] = datetime.now(timezone.utc).replace(tzinfo=None)
    db.sync_status.insert_one(result)
    print("  accounts     %s" % result.get("accounts", "-"))
    print("  transactions %s" % result.get("transactions", "-"))
    print("  status       %s %s" % (result["status"], result.get("error", "")))
    src.close()
    client.close()
    sys.exit(0 if result["status"] == "OK" else 1)


if __name__ == "__main__":
    main()
