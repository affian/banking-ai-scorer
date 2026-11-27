-- bank_tables.sql - tables for the bank demo on Oracle Database Free 23ai
-- Prerequisite: run setup_bank.sql as SYS first. It creates the BANK and
-- BANK_APP users and grants CREATE SEQUENCE to BANK, which the identity column
-- in AI_RISK_SCORE needs. Without that grant this script fails with ORA-01031.
-- Run as BANK:  sqlplus bank@//localhost:10022/FREEPDB1 @bank_tables.sql
--
-- Types mirror the Db2 tables (bank_sync_agent.py):
--   money   NUMBER(13,2)   exact, never float
--   dates   DATE / TIMESTAMP
-- Three extras show the "AI output" and "vector" parts of the question:
--   AI_RISK_SCORE  stores the scorer output; PATTERNS is a native JSON column
--   TXN_FEATURES   one 7-number embedding per transaction, VECTOR(7, FLOAT32)
WHENEVER SQLERROR EXIT FAILURE

CREATE TABLE customer (
  cust_id     NUMBER(10)    NOT NULL PRIMARY KEY,
  full_name   VARCHAR2(60)  NOT NULL,
  city        VARCHAR2(40)  NOT NULL,
  country     CHAR(2)       NOT NULL,
  risk_band   CHAR(1)       NOT NULL,
  opened_date DATE          NOT NULL
);

CREATE TABLE account (
  acct_id     NUMBER(10)    NOT NULL PRIMARY KEY,
  cust_id     NUMBER(10)    NOT NULL REFERENCES customer (cust_id),
  acct_type   CHAR(3)       NOT NULL,
  balance     NUMBER(13,2)  NOT NULL,
  opened_date DATE          NOT NULL,
  status      CHAR(1)       NOT NULL
);

CREATE TABLE txn (
  txn_id      NUMBER(19)    NOT NULL PRIMARY KEY,
  acct_id     NUMBER(10)    NOT NULL REFERENCES account (acct_id),
  txn_ts      TIMESTAMP     NOT NULL,
  amount      NUMBER(13,2)  NOT NULL,
  txn_type    CHAR(3)       NOT NULL,
  country     CHAR(2)       NOT NULL,
  channel     CHAR(3)       NOT NULL
);
CREATE INDEX txn_acct_ts ON txn (acct_id, txn_ts);
CREATE INDEX txn_ts_ix   ON txn (txn_ts);

CREATE TABLE ai_risk_score (
  score_id      NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  acct_id       NUMBER(10)   NOT NULL,
  txn_id        NUMBER(19)   NOT NULL,
  model_version VARCHAR2(20) NOT NULL,
  risk_score    NUMBER(3,2)  NOT NULL,
  risk_level    VARCHAR2(10) NOT NULL,
  is_flagged    NUMBER(1)    NOT NULL CHECK (is_flagged IN (0, 1)),
  patterns      JSON         NOT NULL,
  scored_at     TIMESTAMP    DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT ai_score_uq UNIQUE (txn_id, model_version)
);
CREATE INDEX ai_score_acct ON ai_risk_score (acct_id, scored_at);

CREATE TABLE txn_features (
  txn_id    NUMBER(19) NOT NULL PRIMARY KEY,
  embedding VECTOR(7, FLOAT32) NOT NULL
);

-- scorer account: read the data, write only the AI output table
GRANT SELECT ON customer      TO bank_app;
GRANT SELECT ON account       TO bank_app;
GRANT SELECT ON txn           TO bank_app;
GRANT SELECT ON txn_features  TO bank_app;
GRANT SELECT, INSERT, UPDATE ON ai_risk_score TO bank_app;

SELECT table_name FROM user_tables ORDER BY 1;
EXIT
