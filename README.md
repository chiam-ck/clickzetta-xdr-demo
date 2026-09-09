# ClickZetta synthetic xDR demo

This repository is a sanitized, synthetic demonstration of an xDR mediation
pipeline:

```text
Python xDR generator -> Amazon S3 -> ClickZetta ODS -> dynamic tables
                     -> semantic view -> read-only agent
```

It contains only reusable code, deployment templates, and technical findings.
No production or employer data is used.

## Repository layout

- `source/xdr_source.py` generates deterministic synthetic xDR batches and correction records.
- `sql/clickzetta_ddl.sql` creates the ClickZetta objects.
- `sql/ods_load.sql` is the scheduled S3-to-ODS load task.
- `systemd/` schedules normal and correction batches.
- `n8n/` contains optional SSH wrappers for the systemd units.
- `agent/xdr_agent.py` is an optional read-only consumer-agent harness.
- `findings.md` records observations from the synthetic evaluation.

## Prerequisites

- Python 3.10+
- An S3 bucket and two bucket-scoped identities:
  - uploader: `s3:PutObject` and `s3:ListBucket`
  - ClickZetta reader/loader: `s3:GetObject`, `s3:ListBucket`, `s3:GetBucketLocation`, and `s3:DeleteObject` when using `PURGE=TRUE`
- A ClickZetta workspace and VCluster
- `cz-cli` configured for that workspace

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.clickzetta.example .env.clickzetta
```

Fill `.env.clickzetta` locally. It is ignored by Git. Never commit credentials.

Generate one local batch:

```bash
python source/xdr_source.py --rows 8000
```

Upload a batch after exporting the AWS variables from your local environment:

```bash
python source/xdr_source.py --rows 8000 --s3 s3://YOUR_BUCKET/xdr
```

## ClickZetta setup

1. Replace all `<...>` placeholders in `sql/clickzetta_ddl.sql`.
2. Run the DDL section by section so asynchronous failures are detected before dependent statements run.
3. Execute every write through the audited write path:

```bash
cz-cli sql --write -f sql/clickzetta_ddl.sql
```

4. Configure `sql/ods_load.sql` as a Studio SQL task on a ten-minute schedule.
5. Save raw JSON for every `EXPLAIN` and `SHOW DYNAMIC TABLE REFRESH HISTORY` response outside version control.

`COPY INTO` is append-based; this demo uses `PURGE=TRUE` so the landing prefix acts as a consume-once queue. Verify that the S3 identity can delete objects, because a successful load does not guarantee that purge succeeded.

For host scheduling, see `systemd/INSTALL.md`.

## Consumer agent

Configure a separate ClickZetta profile named `agent` with read-only access, plus the optional OpenAI-compatible endpoint variables in `.env.clickzetta`. Then run:

```bash
python agent/xdr_agent.py "Which cells had the highest data volume today?"
```

Tool-call logs are written under `evidence/`, which is intentionally ignored.

## Security notes

- All generated subscriber identifiers and events are synthetic.
- Keep local secrets in `.env.clickzetta` or the protected host environment file; neither should be committed.
- Replace example bucket, endpoint, region, and identity values with your own.
- Review generated agent logs before sharing them; query results can contain operational metadata even when the source data is synthetic.
