-- verify.sql - parity check against the Db2 source
-- Run as BANK:  sqlplus bank@//localhost:10022/FREEPDB1 @verify.sql
-- Expected: CUSTOMER 200, ACCOUNT 300, TXN 3786, TXN_FEATURES 3786,
--           balance for account 5025 = 1979.75,
--           12 flagged ids: 990001, 990011-990016, 990021-990025
SET PAGESIZE 100 LINESIZE 120
SELECT 'CUSTOMER' AS tbl, COUNT(*) AS n FROM customer
UNION ALL SELECT 'ACCOUNT', COUNT(*) FROM account
UNION ALL SELECT 'TXN', COUNT(*) FROM txn
UNION ALL SELECT 'TXN_FEATURES', COUNT(*) FROM txn_features;

SELECT acct_id, balance, status FROM account WHERE acct_id = 5025;

-- Same three rules as FLAGGED_SQL in cloud_api/app.py, 90 days
SELECT LISTAGG(txn_id, ',') WITHIN GROUP (ORDER BY txn_id) AS flagged_ids,
       COUNT(*) AS flagged_count
  FROM (
    SELECT t.txn_id FROM txn t
     WHERE t.txn_ts >= SYSTIMESTAMP - INTERVAL '90' DAY
       AND t.amount >= 10000 AND t.country <> 'GB'
    UNION ALL
    SELECT t.txn_id FROM txn t JOIN account a ON a.acct_id = t.acct_id
     WHERE t.txn_ts >= SYSTIMESTAMP - INTERVAL '90' DAY AND a.status = 'D'
    UNION ALL
    SELECT t.txn_id FROM txn t
     WHERE t.txn_ts >= SYSTIMESTAMP - INTERVAL '90' DAY
       AND t.txn_type = 'DEP' AND t.amount BETWEEN 9000 AND 9999.99
       AND (SELECT COUNT(*) FROM txn x
             WHERE x.acct_id = t.acct_id AND x.txn_type = 'DEP'
               AND x.amount BETWEEN 9000 AND 9999.99
               AND TRUNC(x.txn_ts) = TRUNC(t.txn_ts)) >= 3
  );
EXIT
