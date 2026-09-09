-- ============================================================
-- ClickZetta xDR demo — DDL kit (run section by section with:
--   cz-cli sql --write -f sql/clickzetta_ddl.sql   or paste into Studio)
-- Verified against ClickZetta documentation and a synthetic test environment:
--   om-storage-connection, om-external-volume, SQL_Copy_Into_Guide,
--   create-dynamic-table, semantic-view-create
-- Replace <...> placeholders. Never commit real keys.
-- ============================================================

-- ---------- 0. schema ----------
CREATE SCHEMA IF NOT EXISTS xdr_demo;

-- ---------- 1. storage connection + external volume ----------
CREATE STORAGE CONNECTION xdr_s3_conn
  TYPE S3
  ACCESS_KEY = '<clickzetta-reader access key>'
  SECRET_KEY = '<clickzetta-reader secret key>'
  REGION = '<your-aws-region>'
  ENDPOINT = 's3.<your-aws-region>.amazonaws.com';

CREATE EXTERNAL VOLUME xdr_demo.xdr_landing
  LOCATION 's3://<your-bucket>/xdr/'
  USING CONNECTION xdr_s3_conn
  DIRECTORY = (ENABLE = TRUE, AUTO_REFRESH = TRUE)
  RECURSIVE = TRUE;

-- sanity
SELECT * FROM DIRECTORY(VOLUME xdr_demo.xdr_landing);
SELECT * FROM VOLUME xdr_demo.xdr_landing USING PARQUET LIMIT 5;
-- if listing lags:  ALTER VOLUME xdr_demo.xdr_landing REFRESH;

-- ---------- 2. ODS ----------
CREATE TABLE IF NOT EXISTS xdr_demo.ods_xdr_raw (
  xdr_id          STRING,
  record_version  INT,
  record_type     STRING,
  msisdn          STRING,
  imsi            STRING,
  cell_id         STRING,
  rat             STRING,
  apn             STRING,
  roaming_partner STRING,
  event_ts        TIMESTAMP,
  ingest_ts       TIMESTAMP,
  duration_s      INT,
  bytes_ul        BIGINT,
  bytes_dl        BIGINT,
  rated_amount    DECIMAL(12,4),
  batch_id        INT,
  batch_type      STRING
);

-- COPY INTO appends files even when a path was loaded before. PURGE requires
-- s3:DeleteObject and makes the landing prefix a consume-once queue.
COPY INTO xdr_demo.ods_xdr_raw
FROM VOLUME xdr_demo.xdr_landing
USING PARQUET
PURGE=TRUE;

SELECT * FROM load_history('xdr_demo.ods_xdr_raw');
SELECT batch_type, COUNT(*) FROM xdr_demo.ods_xdr_raw GROUP BY 1;

-- ---------- 3. DWD (dynamic table) ----------
-- Variant A: window dedupe to latest record_version. EXPLAIN it — this is test T1.
CREATE DYNAMIC TABLE xdr_demo.dwd_session
REFRESH INTERVAL 1 MINUTE
VCLUSTER DEFAULT
AS
SELECT xdr_id, record_version, record_type, msisdn, imsi, cell_id, rat, apn, roaming_partner,
       event_ts, ingest_ts, duration_s, bytes_ul, bytes_dl, rated_amount, batch_id, batch_type,
       date_trunc('hour', event_ts)               AS event_hour,
       timestampdiff(HOUR, event_ts, ingest_ts)   AS lateness_h
FROM (
  SELECT r.*,
         ROW_NUMBER() OVER (PARTITION BY xdr_id ORDER BY record_version DESC, ingest_ts DESC) AS rn
  FROM xdr_demo.ods_xdr_raw r
) t
WHERE rn = 1;

EXPLAIN SELECT * FROM xdr_demo.dwd_session;

-- ---------- 4. ADS (dynamic tables) ----------
CREATE DYNAMIC TABLE xdr_demo.ads_hourly_cell_usage
REFRESH INTERVAL 1 MINUTE
VCLUSTER DEFAULT
AS
SELECT event_hour, cell_id, rat,
       COUNT(*)                      AS sessions,
       COUNT(DISTINCT msisdn)        AS uniq_subs,          -- non-additive on purpose (T1/T4)
       APPROX_COUNT_DISTINCT(msisdn) AS uniq_subs_approx,   -- comparison
       SUM(bytes_dl)                 AS bytes_dl,
       SUM(bytes_ul)                 AS bytes_ul,
       SUM(rated_amount)             AS revenue,
       SUM(CASE WHEN lateness_h >= 24 THEN 1 ELSE 0 END) AS very_late_records
FROM xdr_demo.dwd_session
GROUP BY 1, 2, 3;

CREATE DYNAMIC TABLE xdr_demo.ads_subscriber_daily
REFRESH INTERVAL 5 MINUTE
VCLUSTER DEFAULT
AS
SELECT date(event_ts)              AS event_date,
       msisdn,
       SUM(bytes_dl + bytes_ul)    AS bytes_total,
       SUM(duration_s)             AS voice_s,
       SUM(rated_amount)           AS revenue,
       MAX(lateness_h)             AS max_lateness_h
FROM xdr_demo.dwd_session
GROUP BY 1, 2;

CREATE DYNAMIC TABLE xdr_demo.ads_late_arrival_audit
REFRESH INTERVAL 1 MINUTE
VCLUSTER DEFAULT
AS
SELECT date_trunc('hour', ingest_ts) AS ingest_hour,
       CASE WHEN lateness_h < 1  THEN 'on_time'
            WHEN lateness_h < 6  THEN 'late_1_6h'
            WHEN lateness_h < 24 THEN 'late_6_24h'
            ELSE 'late_24h_plus' END AS lateness_bucket,
       COUNT(*)                    AS records,
       COUNT(DISTINCT event_hour)  AS event_hours_touched
FROM xdr_demo.dwd_session
GROUP BY 1, 2;

EXPLAIN SELECT * FROM xdr_demo.ads_hourly_cell_usage;
EXPLAIN SELECT * FROM xdr_demo.ads_subscriber_daily;
EXPLAIN SELECT * FROM xdr_demo.ads_late_arrival_audit;

SHOW DYNAMIC TABLE REFRESH HISTORY WHERE NAME='ads_hourly_cell_usage';
SHOW DYNAMIC TABLE REFRESH HISTORY WHERE NAME='dwd_session';

-- ---------- 5. Semantic view ----------
-- clause order is fixed: TABLES → RELATIONSHIPS → VARIABLES → FACTS → DIMENSIONS → METRICS
CREATE OR REPLACE SEMANTIC VIEW xdr_demo.sv_network_usage
TABLES (
  usage AS xdr_demo.ads_hourly_cell_usage
    PRIMARY KEY (event_hour, cell_id, rat)
    WITH SYNONYMS ('cell usage', 'network usage', 'hourly usage')
    COMMENT = 'Hourly usage per cell and radio technology, from deduplicated xDRs'
)
VARIABLES (
  late_threshold_records INT DEFAULT 1 COMMENT = 'records >=24h late that flag a cell-hour as unreliable'
)
DIMENSIONS (
  usage.event_hour AS usage.event_hour
    is_time = true
    COMMENT = 'Hour the sessions happened',
  usage.cell AS usage.cell_id
    WITH SYNONYMS = ('cell', 'site', 'cell id')
    COMMENT = 'Serving cell',
  usage.radio AS usage.rat
    WITH SYNONYMS = ('RAT', 'technology')
    enum_values = ['4G', '5G']
    COMMENT = 'Radio access technology',
  usage.reliability AS CASE WHEN usage.very_late_records >= late_threshold_records THEN 'PROVISIONAL' ELSE 'SETTLED' END
    COMMENT = 'Whether late roaming records may still change this hour'
)
METRICS (
  usage.sessions           AS SUM(usage.sessions)        COMMENT = 'Session count',
  usage.unique_subscribers AS SUM(usage.uniq_subs)       COMMENT = 'Sum of per-cell distinct subscribers (overcounts across cells by design; see notes)',
  usage.data_gb            AS SUM(usage.bytes_dl + usage.bytes_ul) / 1073741824.0 COMMENT = 'Total data volume in GB',
  usage.revenue            AS SUM(usage.revenue)         COMMENT = 'Rated revenue',
  usage.gb_per_session     AS usage.data_gb / usage.sessions COMMENT = 'Derived: average GB per session'
)
COMMENT = 'Network usage semantic layer for analysts and agents';

SELECT * FROM semantic_view(
  xdr_demo.sv_network_usage
  DIMENSIONS usage.cell, usage.radio
  METRICS usage.sessions, usage.data_gb, usage.revenue
) ORDER BY data_gb DESC LIMIT 10;

SELECT * FROM semantic_view(
  xdr_demo.sv_network_usage
  DIMENSIONS usage.event_hour, usage.reliability
  METRICS usage.sessions, usage.revenue
) ORDER BY event_hour DESC LIMIT 24;

DESC EXTENDED xdr_demo.sv_network_usage;

-- ---------- 6. Governance (T6) ----------
-- Fill in from Studio docs: column masking policy on msisdn + a read-only role for the agent.
-- Record exact syntax used / whether UI-only as a finding.
