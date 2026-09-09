# Host scheduler setup

Run these commands on the Linux host that generates the synthetic xDR data. The
example units use the dedicated user `xdr-demo`, install the app in
`/opt/xdr-source`, and load secrets from `/etc/xdr-demo.env`.

```bash
sudo useradd --system --home /opt/xdr-source --shell /usr/sbin/nologin xdr-demo
sudo mkdir -p /opt/xdr-source
sudo cp source/xdr_source.py /opt/xdr-source/
sudo python3 -m venv /opt/xdr-source/.venv
sudo /opt/xdr-source/.venv/bin/pip install pyarrow boto3
sudo chown -R xdr-demo:xdr-demo /opt/xdr-source
```

Create `/etc/xdr-demo.env` with bucket-scoped uploader credentials. Do not add it
to this repository:

```dotenv
AWS_ACCESS_KEY_ID=<uploader-access-key>
AWS_SECRET_ACCESS_KEY=<uploader-secret-key>
AWS_DEFAULT_REGION=<aws-region>
XDR_BUCKET=<bucket-name>
```

Protect the environment file and install the units:

```bash
sudo chown root:xdr-demo /etc/xdr-demo.env
sudo chmod 0640 /etc/xdr-demo.env
sudo cp systemd/xdr-mediation.service systemd/xdr-mediation.timer systemd/xdr-correction.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xdr-mediation.timer
systemctl list-timers xdr-mediation.timer
```

Manual runs and logs:

```bash
sudo systemctl start xdr-mediation.service
sudo systemctl start xdr-correction.service
journalctl -u xdr-mediation.service -n 20
```
