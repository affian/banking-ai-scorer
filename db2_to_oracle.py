#!/usr/bin/env python3
"""
db2_to_oracle.py - copy BANKDEMO from Db2 on IBM Cloud into Oracle Database

  Db2 on Cloud (ibm_db, TLS)  --read-->  this script  --write-->  Oracle (python-oracledb, Thin)

TABLES (same names and types as the Db2 side, see bank_tables.sql)
  CUSTOMER, ACCOUNT, TXN   copied with MERGE (upsert by primary key)
  TXN_FEATURES             one 7-number VECTOR per transaction, built here

BEHAVIOUR
  CUSTOMER, ACCOUNT  full refresh, upserted by key
  TXN                incremental: rows at or after (newest TXN_TS in Oracle - 1 day).
                     Re-reading is harmless (idempotent).
  --full             ignore the watermark and resend all transactions

USAGE
  python db2_to_oracle.py --check     test both connections, change nothing
  python db2_to_oracle.py             run one sync
  python db2_to_oracle.py --full

CONFIG (environment or .env next to this file)
  CLOUD_HOST CLOUD_PORT CLOUD_DB CLOUD_USER CLOUD_PASSWORD    Db2 source
  ORACLE_DSN ORACLE_USER ORACLE_PASSWORD                      Oracle target
  ORACLE_DSN example:  localhost:10022/FREEPDB1   (host:port/service)
  Passwords are never printed.
"""
import argparse
import array
import os
import sys
from datetime import datetime, timedelta

BATCH = 500
OVERLAP = timedelta(days=1)
TYPES = ("DEP", "WDR", "PAY", "XFR")


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
    for name in ("CLOUD_PASSWORD", "ORACLE_PASSWORD"):
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


def connect_oracle():
    import oracledb
    return oracledb.connect(user=os.environ["ORACLE_USER"],
                            password=os.environ["ORACLE_PASSWORD"],
                            dsn=os.environ["ORACLE_DSN"])


def feature_vector(amount, country, txn_type, ts):
    """7 numbers, all 0..1: amount, foreign flag, 4 one-hot types, hour of day."""
    v = [min(float(amount) / 50000.0, 1.0),
         0.0 if country.strip() == "GB" else 1.0]
    v += [1.0 if txn_type.strip() == t else 0.0 for t in TYPES]
    v.append(ts.hour / 23.0)
    return array.array("f", v)


MERGE_CUSTOMER = (
    "MERGE INTO customer t USING (SELECT :1 cust_id, :2 full_name, :3 city, :4 country, "
    ":5 risk_band, :6 opened_date FROM dual) s ON (t.cust_id = s.cust_id) "
    "WHEN MATCHED THEN UPDATE SET t.full_name = s.full_name, t.city = s.city, "
    "t.country = s.country, t.risk_band = s.risk_band, t.opened_date = s.opened_date "
    "WHEN NOT MATCHED THEN INSERT (cust_id, full_name, city, country, risk_band, opened_date) "
    "VALUES (s.cust_id, s.full_name, s.city, s.country, s.risk_band, s.opened_date)")

MERGE_ACCOUNT = (
    "MERGE INTO account t USING (SELECT :1 acct_id, :2 cust_id, :3 acct_type, :4 balance, "
    ":5 opened_date, :6 status FROM dual) s ON (t.acct_id = s.acct_id) "
    "WHEN MATCHED THEN UPDATE SET t.cust_id = s.cust_id, t.acct_type = s.acct_type, "
    "t.balance = s.balance, t.opened_date = s.opened_date, t.status = s.status "
    "WHEN NOT MATCHED THEN INSERT (acct_id, cust_id, acct_type, balance, opened_date, status) "
    "VALUES (s.acct_id, s.cust_id, s.acct_type, s.balance, s.opened_date, s.status)")

MERGE_TXN = (
    "MERGE INTO txn t USING (SELECT :1 txn_id, :2 acct_id, :3 txn_ts, :4 amount, :5 txn_type, "
    ":6 country, :7 channel FROM dual) s ON (t.txn_id = s.txn_id) "
    "WHEN MATCHED THEN UPDATE SET t.acct_id = s.acct_id, t.txn_ts = s.txn_ts, "
    "t.amount = s.amount, t.txn_type = s.txn_type, t.country = s.country, t.channel = s.channel "
    "WHEN NOT MATCHED THEN INSERT (txn_id, acct_id, txn_ts, amount, txn_type, country, channel) "
    "VALUES (s.txn_id, s.acct_id, s.txn_ts, s.amount, s.txn_type, s.country, s.channel)")

# vectors: delete then insert (keeps the VECTOR bind out of a MERGE ... FROM dual)
DELETE_FEATURE = "DELETE FROM txn_features WHERE txn_id = :1"
INSERT_FEATURE = "INSERT INTO txn_features (txn_id, embedding) VALUES (:1, :2)"


def copy_simple(src, dst, select_sql, merge_sql, args=()):
    cur = src.cursor()
    cur.execute(select_sql, args)
    out = dst.cursor()
    sent = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        out.executemany(merge_sql, [tuple(r) for r in rows])
        dst.commit()
        sent += len(rows)
    return sent


def sync_txn(src, dst, full):
    args = ()
    where = ""
    if not full:
        c = dst.cursor()
        c.execute("SELECT MAX(txn_ts) FROM txn")
        newest = c.fetchone()[0]
        if newest:
            where = " WHERE TXN_TS >= ?"
            args = (newest - OVERLAP,)
    cur = src.cursor()
    cur.execute("SELECT TXN_ID, ACCT_ID, TXN_TS, AMOUNT, TXN_TYPE, COUNTRY, CHANNEL "
                "FROM TXN" + where + " ORDER BY TXN_ID", args)
    out = dst.cursor()
    sent = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        out.executemany(MERGE_TXN, [tuple(r) for r in rows])
        out.executemany(DELETE_FEATURE, [(r[0],) for r in rows])
        out.executemany(INSERT_FEATURE,
                        [(r[0], feature_vector(r[3], r[5], r[4], r[2])) for r in rows])
        dst.commit()
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
          "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD"])

    try:
        src = connect_db2()
        print("Db2 cloud: connected")
    except Exception as exc:
        sys.exit("Db2 connection failed: " + scrub(str(exc))[:300])
    try:
        dst = connect_oracle()
        print("Oracle   : connected, version " + dst.version)
    except Exception as exc:
        sys.exit("Oracle connection failed: " + scrub(str(exc))[:300])

    if args.check:
        print("Check passed. Nothing was changed.")
        return

    started = datetime.now()
    try:
        n_cust = copy_simple(
            src, dst,
            "SELECT CUST_ID, FULL_NAME, CITY, COUNTRY, RISK_BAND, OPENED_DATE "
            "FROM CUSTOMER ORDER BY CUST_ID", MERGE_CUSTOMER)
        n_acct = copy_simple(
            src, dst,
            "SELECT ACCT_ID, CUST_ID, ACCT_TYPE, BALANCE, OPENED_DATE, STATUS "
            "FROM ACCOUNT ORDER BY ACCT_ID", MERGE_ACCOUNT)
        n_txn = sync_txn(src, dst, args.full)
    except Exception as exc:
        sys.exit("Sync failed: " + scrub(str(exc))[:500])
    print("  customers    %s" % n_cust)
    print("  accounts     %s" % n_acct)
    print("  transactions %s (and the same number of vectors)" % n_txn)
    print("  seconds      %.1f" % (datetime.now() - started).total_seconds())
    src.close()
    dst.close()


if __name__ == "__main__":
    main()
