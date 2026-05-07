# Deploying Harvest

This is the operational runbook for getting Harvest from "tests pass" to
"running 24/7 in production."

The recommended path is **Hetzner Cloud + systemd**. Total cost: ~$5/month,
total time: ~30 minutes.

---

## 0. Prerequisites checklist

You need:

- [ ] A Public.com brokerage account with **Options Level 2+ enabled**
- [ ] A Public.com **API secret** (Settings → API → Generate)
- [ ] Your Public.com **account ID** (visible in API portal or first auth response)
- [ ] A Polygon (Massive) **API key** with options + indices entitlements
- [ ] A Discord server you control, and a **webhook URL** in the channel where
      you want alerts (Server Settings → Integrations → Webhooks → New Webhook)
- [ ] An SSH public key on your laptop (`~/.ssh/id_ed25519.pub` or similar)

---

## 1. Provision the VPS (5 minutes)

1. Sign up at <https://www.hetzner.com/cloud>
2. Create a new project called **Harvest**
3. **Add SSH Key** under Security: paste your public key
4. **Create Server**:
   - Location: **Ashburn, VA** (lowest latency to NYSE)
   - Image: **Ubuntu 22.04**
   - Type: **CX22** (~€4.50/mo, plenty for this workload)
   - SSH key: pick the one you just added
5. After ~30s the server is up. Note the public IP.

SSH in:

```bash
ssh root@<your-server-ip>
```

---

## 2. Install Harvest (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/VincentDelisi/Harvest/main/deploy/install.sh | sudo bash
```

This:
- Installs Python 3.11, git, tzdata
- Sets the system timezone to America/New_York
- Creates a non-root `harvest` user
- Clones the repo into `/opt/harvest`
- Sets up a virtualenv at `/opt/harvest/.venv` with all deps
- Creates `/opt/harvest/.env` from the template
- Installs the `harvest.service` systemd unit (but doesn't start it yet)

> Note: until the repo is public, replace the curl command with a manual `git clone` using a deploy key.

---

## 3. Configure credentials

```bash
sudo -u harvest nano /opt/harvest/.env
```

Fill in:

```dotenv
PUBLIC_COM_SECRET=...
PUBLIC_COM_ACCOUNT_ID=...
POLYGON_API_KEY=...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
ENGINE_MODE=DRY_RUN          # ← always start here
LOG_LEVEL=INFO
```

Save and exit. The file is mode `600` and owned by `harvest`.

---

## 4. Verify connectivity (DRY_RUN smoke test)

```bash
sudo -u harvest /opt/harvest/.venv/bin/python -m scripts.check_today
```

You should see:
- ✅ Event blackout check
- ✅ VIX value
- ✅ Per-underlying SMA50/SMA200, regime, current RSI(2), bootstrap IVR/IVP

If any row says `ERR`, fix that before continuing.

Then a single engine tick:

```bash
sudo -u harvest /opt/harvest/.venv/bin/python -m scripts.run_engine --once --dry-run
```

You should get a Discord ping with a "Morning context" embed.

---

## 5. Start the service

```bash
sudo systemctl enable --now harvest
sudo systemctl status harvest
```

You're now running. Tail the logs:

```bash
sudo tail -f /var/log/harvest/engine.log
```

---

## 6. Promote to live trading (later)

After at least **5 trading days in DRY_RUN** with logs you trust:

```bash
sudo -u harvest sed -i 's/ENGINE_MODE=DRY_RUN/ENGINE_MODE=LIVE_SMALL/' /opt/harvest/.env
sudo systemctl restart harvest
```

`LIVE_SMALL` caps every position at 1 contract. Stay there for 30 trades.
After review:

```bash
sudo -u harvest sed -i 's/ENGINE_MODE=LIVE_SMALL/ENGINE_MODE=LIVE/' /opt/harvest/.env
sudo systemctl restart harvest
```

---

## 7. Operational commands

```bash
# Status
sudo systemctl status harvest

# Stop / start / restart
sudo systemctl stop harvest
sudo systemctl start harvest
sudo systemctl restart harvest

# Logs (live tail)
tail -f /var/log/harvest/engine.log
tail -f /var/log/harvest/engine.err.log

# Manually reset the kill switch (after investigating)
sudo -u harvest /opt/harvest/.venv/bin/python -m scripts.reset_kill_switch

# Update to latest code from GitHub
cd /opt/harvest && sudo -u harvest git pull && sudo systemctl restart harvest
```

---

## 8. Backups

The SQLite database at `/opt/harvest/data/engine.db` is your trade ledger.
Back it up nightly:

```bash
sudo crontab -e
# Add:
0 2 * * * cp /opt/harvest/data/engine.db /opt/harvest/data/engine.db.$(date +\%F).bak && find /opt/harvest/data/engine.db.*.bak -mtime +14 -delete
```

For off-box backups, consider rclone to a cheap S3 / B2 bucket.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Service crashes on start | Bad `.env` value or missing key | Run `--once --dry-run` manually; check stderr |
| No Discord alerts | Wrong webhook URL or rate-limited | Test with `curl -X POST ${URL} -H 'Content-Type: application/json' -d '{"content":"ping"}'` |
| `Polygon 403` errors | API key lacks options/indices | Check Polygon dashboard entitlements |
| `Public 401` errors | Secret expired or wrong | Regenerate secret in Public dashboard |
| Kill switch keeps tripping | API errors or VIX spike | Check logs, fix underlying issue, then run reset script |
| Time on box is wrong | NTP not running | `sudo systemctl status systemd-timesyncd` |
