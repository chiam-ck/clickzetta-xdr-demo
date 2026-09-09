-- Studio task: ods_xdr_load (SQL), cron every 10 min: 0 */10 * * * ? *
-- PURGE=TRUE because COPY INTO is NOT idempotent by file path (see findings.md):
-- each file is loaded exactly once, then deleted from the landing bucket.
ALTER VOLUME xdr_demo.xdr_landing REFRESH;
COPY INTO xdr_demo.ods_xdr_raw
FROM VOLUME xdr_demo.xdr_landing
USING PARQUET
PURGE=TRUE;
