#!/usr/bin/env python3
"""
xdr_source.py — homelab "mediation" simulator for the ClickZetta demo.

Each run emits ONE mediation batch (parquet) covering the next hourly ingest
window, keeps state in a JSON file, and optionally uploads to S3.

    python3 xdr_source.py --rows 8000                       # local only
    python3 xdr_source.py --rows 8000 --s3 s3://BUCKET/xdr  # + upload
    python3 xdr_source.py --correction --s3 s3://BUCKET/xdr # emit a re-rating batch

Batch types
  NORMAL      ~90% on-time (event within the ingest hour), ~8% late 1–6h,
              ~2% very late 24–72h (roaming clearing house).
  CORRECTION  re-emits N previously-issued xdr_ids with a NEW rated_amount and
              record_version=2. Downstream must treat this as an UPDATE, not an
              insert — this is the retraction test for dynamic tables.

Layout in S3 (Hive-style so ClickZetta's volume directory can filter by date):
  <prefix>/dt=YYYY-MM-DD/hour=HH/batch_<seq>_<type>.parquet
"""
import argparse, json, os, random, uuid, sys
from datetime import datetime, timedelta
import pyarrow as pa
import pyarrow.parquet as pq

STATE = "state.json"
SUBS  = 2000
CELLS = 400

SCHEMA = pa.schema([
    ("xdr_id", pa.string()),
    ("record_version", pa.int32()),
    ("record_type", pa.string()),
    ("msisdn", pa.string()),
    ("imsi", pa.string()),
    ("cell_id", pa.string()),
    ("rat", pa.string()),
    ("apn", pa.string()),
    ("roaming_partner", pa.string()),
    ("event_ts", pa.timestamp("us")),
    ("ingest_ts", pa.timestamp("us")),
    ("duration_s", pa.int32()),
    ("bytes_ul", pa.int64()),
    ("bytes_dl", pa.int64()),
    ("rated_amount", pa.decimal128(12, 4)),
    ("batch_id", pa.int32()),
    ("batch_type", pa.string()),
])

def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"seq": 0, "start": datetime.utcnow().replace(minute=0, second=0, microsecond=0).isoformat(),
            "issued": []}   # issued: sample of (xdr_id, event_ts, msisdn, cell_id, rat, record_type, amount)

def save_state(s):
    json.dump(s, open(STATE, "w"))

def rng_for(seq): return random.Random(1000 + seq)

def make_normal(seq, state, n):
    r = rng_for(seq)
    start = datetime.fromisoformat(state["start"])
    ingest_lo = start + timedelta(hours=seq)
    subs  = [f"65{r.randint(80000000, 99999999)}" for _ in range(SUBS)]
    cells = [f"SG-C-{i:05d}" for i in range(CELLS)]
    rows, sample = [], []
    for _ in range(n):
        ingest_ts = ingest_lo + timedelta(seconds=r.randint(0, 3599))
        p = r.random()
        if p < 0.90:
            event_ts = max(ingest_lo, ingest_ts - timedelta(seconds=r.randint(0, 900))); partner = ""
        elif p < 0.98:
            event_ts = ingest_ts - timedelta(hours=r.randint(1, 6), seconds=r.randint(0, 3599)); partner = ""
        else:
            event_ts = ingest_ts - timedelta(hours=r.randint(24, 72)); partner = r.choice(["MY-CELCOM","ID-TSEL","TH-AIS","AU-TELSTRA"])
        if event_ts < start: event_ts = start + timedelta(seconds=r.randint(0, 600))
        rt = r.choices(["DATA","VOICE","SMS"], weights=[80,15,5])[0]
        m  = r.choice(subs); cell = r.choice(cells); rat = r.choices(["4G","5G"], weights=[45,55])[0]
        dur = r.randint(5, 1800) if rt == "VOICE" else 0
        ul  = r.randint(1_000, 5_000_000) if rt == "DATA" else 0
        dl  = r.randint(10_000, 80_000_000) if rt == "DATA" else 0
        amt = round(dur/60*0.05 + (ul+dl)/1e9*8 + (0.05 if rt == "SMS" else 0) + (2.0 if partner else 0), 4)
        xid = uuid.uuid4().hex
        rows.append((xid, 1, rt, m, "52501"+m[2:], cell, rat, r.choice(["internet","ims","enterprise-vpn","iot.m2m"]) if rt=="DATA" else "",
                     partner, event_ts, ingest_ts, dur, ul, dl, amt, seq, "NORMAL"))
        if r.random() < 0.01:
            sample.append([xid, event_ts.isoformat(), m, "52501"+m[2:], cell, rat, rt, dur, ul, dl, amt])
    state["issued"] = (state["issued"] + sample)[-5000:]
    return rows

def make_correction(seq, state, n):
    r = rng_for(seq)
    if not state["issued"]:
        sys.exit("no issued records to correct yet — run a NORMAL batch first")
    now = datetime.utcnow().replace(microsecond=0)
    picks = r.sample(state["issued"], min(n, len(state["issued"])))
    rows = []
    for xid, ets, m, imsi, cell, rat, rt, dur, ul, dl, amt in picks:
        new_amt = round(float(amt) * r.choice([0.0, 0.5, 1.25]), 4)   # refund / partial / uplift
        rows.append((xid, 2, rt, m, imsi, cell, rat, "", "", datetime.fromisoformat(ets), now,
                     dur, ul, dl, new_amt, seq, "CORRECTION"))
    return rows

def write(rows, seq, btype, out, s3):
    from decimal import Decimal
    cols = list(zip(*rows))
    arrays = [pa.array(cols[i], type=SCHEMA.field(i).type) if SCHEMA.field(i).type != pa.decimal128(12,4)
              else pa.array([Decimal(str(v)) for v in cols[i]], type=pa.decimal128(12,4))
              for i in range(len(SCHEMA))]
    t = pa.table(arrays, schema=SCHEMA)
    ing = min(cols[10])
    rel = f"dt={ing:%Y-%m-%d}/hour={ing:%H}/batch_{seq:04d}_{btype}.parquet"
    path = os.path.join(out, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(t, path)
    print(f"wrote {len(rows):,} rows → {path}")
    if s3:
        import boto3
        b, _, prefix = s3.replace("s3://","").partition("/")
        key = f"{prefix.rstrip('/')}/{rel}" if prefix else rel
        boto3.client("s3").upload_file(path, b, key)
        print(f"uploaded → s3://{b}/{key}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=8000)
    ap.add_argument("--correction", action="store_true")
    ap.add_argument("--out", default="out")
    ap.add_argument("--s3", default=None, help="s3://bucket/prefix")
    a = ap.parse_args()
    st = load_state()
    seq = st["seq"]
    rows = make_correction(seq, st, min(a.rows, 200)) if a.correction else make_normal(seq, st, a.rows)
    write(rows, seq, "CORRECTION" if a.correction else "NORMAL", a.out, a.s3)
    st["seq"] = seq + 1
    save_state(st)
