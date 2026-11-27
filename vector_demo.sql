-- vector_demo.sql - AI Vector Search on Oracle Database Free 23ai
-- Run as BANK:  sqlplus bank@//localhost:10022/FREEPDB1 @vector_demo.sql
-- Needs TXN_FEATURES loaded by db2_to_oracle.py.
-- Feature order (7 numbers): amount/50000 (max 1), foreign flag, DEP, WDR,
--                            PAY, XFR (one-hot), hour of day / 23
SET PAGESIZE 100 LINESIZE 140
COLUMN txn_id FORMAT 99999999
COLUMN dist FORMAT 0.9999

PROMPT == 1. VECTOR type is available
SELECT vector_dimension_count(embedding) AS dims, COUNT(*) AS n
  FROM txn_features GROUP BY vector_dimension_count(embedding);

PROMPT == 2. Ten nearest neighbours of flagged transaction 990001 (cosine)
SELECT f.txn_id, t.amount, t.country, t.txn_type,
       VECTOR_DISTANCE(f.embedding,
         (SELECT embedding FROM txn_features WHERE txn_id = 990001), COSINE) AS dist
  FROM txn_features f JOIN txn t ON t.txn_id = f.txn_id
 WHERE f.txn_id <> 990001
 ORDER BY dist
 FETCH FIRST 10 ROWS ONLY;

PROMPT == 3. Approximate search with an IVF index (needs no vector memory pool)
CREATE VECTOR INDEX txn_emb_ivf ON txn_features (embedding)
  ORGANIZATION NEIGHBOR PARTITIONS DISTANCE COSINE;
SELECT f.txn_id,
       VECTOR_DISTANCE(f.embedding,
         (SELECT embedding FROM txn_features WHERE txn_id = 990001), COSINE) AS dist
  FROM txn_features f
 WHERE f.txn_id <> 990001
 ORDER BY dist
 FETCH APPROX FIRST 10 ROWS ONLY;
EXIT
