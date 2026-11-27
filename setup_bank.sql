-- setup_bank.sql - create the two Oracle users for the bank demo
-- Run ONCE as SYS on the VM:   sqlplus / as sysdba @setup_bank.sql
-- It asks for two passwords (hidden). Use letters and digits only.
--
--   BANK      owns the tables, used by the loader (db2_to_oracle.py)
--   BANK_APP  used by the scorer, read on the data, write on AI_RISK_SCORE only
WHENEVER SQLERROR EXIT FAILURE
ALTER SESSION SET CONTAINER = FREEPDB1;
SET VERIFY OFF
ACCEPT bank_pw CHAR PROMPT 'Password for BANK: ' HIDE
ACCEPT app_pw CHAR PROMPT 'Password for BANK_APP: ' HIDE
CREATE USER bank IDENTIFIED BY "&bank_pw" DEFAULT TABLESPACE users QUOTA 500M ON users;
GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO bank;
CREATE USER bank_app IDENTIFIED BY "&app_pw";
GRANT CREATE SESSION TO bank_app;
SELECT username FROM dba_users WHERE username IN ('BANK', 'BANK_APP') ORDER BY 1;
EXIT
