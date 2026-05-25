# 🚀 ORB Intraday Trading System — Deployment Guide

> **Target**: Oracle Cloud Infrastructure (OCI) Always Free Tier  
> **Architecture**: Single Docker container running FastAPI + Streamlit + Strategy Engine  
> **Estimated setup time**: 30–45 minutes

---

## Table of Contents

1. [Create Oracle Cloud Account](#1-create-oracle-cloud-account)
2. [Launch Always Free VM](#2-launch-always-free-vm)
3. [Configure Security Lists (Firewall)](#3-configure-security-lists-firewall)
4. [SSH into the VM](#4-ssh-into-the-vm)
5. [Install Docker](#5-install-docker)
6. [Upload Project Files](#6-upload-project-files)
7. [Configure Environment Variables](#7-configure-environment-variables)
8. [Build and Run](#8-build-and-run)
9. [Verify Deployment](#9-verify-deployment)
10. [Auto-start on Reboot](#10-auto-start-on-reboot)
11. [Monitoring & Logs](#11-monitoring--logs)
12. [Updating the System](#12-updating-the-system)
13. [Firewall Hardening (Optional)](#13-firewall-hardening-optional)
14. [Daily Token Update](#14-daily-token-update)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Create Oracle Cloud Account

1. Go to [cloud.oracle.com](https://cloud.oracle.com) and click **Start for Free**.
2. Sign up with your email and a valid credit/debit card (no charges for Always Free resources).
3. Select your **Home Region** — choose the one closest to India (e.g., `ap-mumbai-1`).
4. Wait for account provisioning (usually 5–15 minutes).

> ⚠️ **Important**: Always Free resources are only available in your **Home Region**. Choose wisely — it cannot be changed later.

---

## 2. Launch Always Free VM

1. Log in to the OCI Console → **Compute → Instances → Create Instance**.
2. Configure the instance:

| Setting | Value |
|---------|-------|
| **Name** | `orb-trading-bot` |
| **Compartment** | root (default) |
| **Availability Domain** | Any available |
| **Image** | Canonical Ubuntu 22.04 (aarch64) |
| **Shape** | `VM.Standard.A1.Flex` |
| **OCPUs** | 1 |
| **Memory (GB)** | 6 |
| **Boot Volume** | 50 GB |

3. Under **Networking**:
   - Select or create a VCN with a public subnet.
   - Ensure **Assign a public IPv4 address** is checked.

4. Under **Add SSH keys**:
   - Upload your public SSH key (`~/.ssh/id_rsa.pub`) or generate a new key pair.
   - **Download and save** the private key — you won't be able to retrieve it later.

5. Click **Create** and wait for the instance to reach **RUNNING** state.
6. Note the **Public IP Address** from the instance details page.

---

## 3. Configure Security Lists (Firewall)

OCI uses **Security Lists** on the VCN subnet to control traffic. You need to open ports for the webhook and dashboard.

1. Go to **Networking → Virtual Cloud Networks** → click your VCN.
2. Click the **public subnet** → click the **Default Security List**.
3. Click **Add Ingress Rules** and add these rules:

| Source CIDR | Protocol | Dest Port | Description |
|-------------|----------|-----------|-------------|
| `0.0.0.0/0` | TCP | 22 | SSH |
| `0.0.0.0/0` | TCP | 8000 | FastAPI Webhook |
| `0.0.0.0/0` | TCP | 8501 | Streamlit Dashboard |

4. Click **Add Ingress Rules** to save.

> 🔒 **Production tip**: Replace `0.0.0.0/0` with your home/office IP (e.g., `203.0.113.5/32`) for better security.

---

## 4. SSH into the VM

```bash
# Set correct permissions on the private key
chmod 400 ~/path/to/private_key.pem

# Connect
ssh -i ~/path/to/private_key.pem ubuntu@<PUBLIC_IP>
```

Verify you're connected:

```bash
uname -a
# Should show: Linux ... aarch64 ...
```

---

## 5. Install Docker

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Install Docker
sudo apt install -y docker.io docker-compose-v2

# Enable Docker service
sudo systemctl enable docker
sudo systemctl start docker

# Add your user to the docker group (avoids needing sudo)
sudo usermod -aG docker ubuntu

# Apply group change (or log out and back in)
newgrp docker

# Verify installation
docker --version
docker compose version
```

Expected output:

```
Docker version 24.x.x, build ...
Docker Compose version v2.x.x
```

---

## 6. Upload Project Files

### Option A: Git Clone (recommended)

If your code is in a Git repository:

```bash
mkdir ~/orb-trader && cd ~/orb-trader
git clone https://github.com/YOUR_USER/orb-trader.git .
```

### Option B: SCP Upload

From your **local machine**:

```bash
# Create the directory on the server
ssh -i key.pem ubuntu@<IP> "mkdir -p ~/orb-trader"

# Upload all project files
scp -i key.pem -r ./* ubuntu@<IP>:~/orb-trader/
```

### Verify files are present

```bash
cd ~/orb-trader
ls -la

# You should see:
#   main.py
#   config.py
#   database.py
#   broker.py
#   market_feed.py
#   strategy.py
#   risk_manager.py
#   notifier.py
#   webhook.py
#   dashboard.py
#   utils.py
#   nifty50.json
#   requirements.txt
#   Dockerfile
#   docker-compose.yml
#   .env.example
```

---

## 7. Configure Environment Variables

```bash
cd ~/orb-trader

# Copy the example env file
cp .env.example .env

# Edit with your credentials
nano .env
```

Fill in the following values:

```env
# ── Dhan Broker ──
DHAN_CLIENT_ID=your_client_id_here
DHAN_ACCESS_TOKEN=your_daily_access_token

# ── Telegram ──
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=-1001234567890

# ── Trading Settings ──
BASE_CAPITAL=5000
LEVERAGE=5
MAX_RANGE_PCT=1.5
MIN_STOCK_PRICE=60
PAPER_TRADING=true

# ── Security ──
WEBHOOK_SECRET=your_random_secret_string_here
DASHBOARD_PASSWORD=your_dashboard_password

# ── Paths ──
DB_PATH=data/trades.db
```

> 💡 **Tip**: Generate a secure webhook secret:
> ```bash
> python3 -c "import secrets; print(secrets.token_urlsafe(32))"
> ```

Save and exit (`Ctrl+X`, then `Y`, then `Enter`).

---

## 8. Build and Run

```bash
cd ~/orb-trader

# Create data and logs directories
mkdir -p data logs

# Build the Docker image and start
docker compose up -d --build

# Watch the build progress
docker compose logs -f
```

The build takes 2–5 minutes on the first run. You'll see:

```
orb-trading-bot  | ╔════════════════════════════════════════════════════════╗
orb-trading-bot  | ║   ORB INTRADAY TRADING SYSTEM — Starting up…         ║
orb-trading-bot  | ╚════════════════════════════════════════════════════════╝
orb-trading-bot  | Initialising core components…
orb-trading-bot  | FastAPI webhook server started on :8000
orb-trading-bot  | Streamlit dashboard started on :8501
orb-trading-bot  | System running. Waiting for scheduled events…
```

Press `Ctrl+C` to stop following logs (the container keeps running).

---

## 9. Verify Deployment

### Health Check

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "uptime_seconds": 42.5,
  "uptime_human": "42s",
  "started_at": "2025-01-15T09:00:00+05:30",
  "server_time": "2025-01-15T09:00:42+05:30",
  "paper_trading": true
}
```

### Dashboard

Open in your browser:

```
http://<PUBLIC_IP>:8501
```

Enter your dashboard password to access the trading interface.

### API Status

```bash
curl http://localhost:8000/api/status
```

---

## 10. Auto-start on Reboot

Docker's `restart: unless-stopped` policy in `docker-compose.yml` ensures the container automatically starts when the VM reboots.

Verify:

```bash
# Simulate a reboot
sudo reboot

# After reconnecting via SSH:
docker compose ps
# Should show "Up" status
```

---

## 11. Monitoring & Logs

### View live logs

```bash
cd ~/orb-trader
docker compose logs -f --tail 100
```

### View only errors

```bash
docker compose logs -f 2>&1 | grep -i "error\|warning\|exception"
```

### Check container status

```bash
docker compose ps
docker stats orb-trading-bot
```

### Check disk usage

```bash
du -sh data/ logs/
df -h
```

### Log rotation

Logs are automatically rotated by Docker (max 10MB × 5 files) as configured in `docker-compose.yml`.

---

## 12. Updating the System

```bash
cd ~/orb-trader

# Stop the running container
docker compose down

# Pull/upload updated files
# Option A: git pull
git pull origin main

# Option B: re-upload via scp
# scp -i key.pem -r ./* ubuntu@<IP>:~/orb-trader/

# Rebuild and restart
docker compose up -d --build

# Verify
docker compose logs -f --tail 50
```

### Zero-downtime considerations

Since this is a single-instance trading bot (not a web service requiring HA), a brief downtime during updates is acceptable. Schedule updates **outside market hours** (before 09:00 or after 16:00 IST).

---

## 13. Firewall Hardening (Optional)

For additional security beyond OCI Security Lists, enable UFW on the VM:

```bash
# Install UFW
sudo apt install -y ufw

# Default policies
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow SSH
sudo ufw allow 22/tcp

# Allow webhook and dashboard
sudo ufw allow 8000/tcp
sudo ufw allow 8501/tcp

# Enable UFW
sudo ufw enable

# Verify
sudo ufw status verbose
```

### Restrict to specific IPs

```bash
# Only allow your IP to access dashboard
sudo ufw allow from 203.0.113.5 to any port 8501
sudo ufw deny 8501/tcp  # Deny all others
```

---

## 14. Daily Token Update

Dhan access tokens expire daily. You have two options for updating:

### Option A: Dashboard UI (Recommended)

1. Open `http://<PUBLIC_IP>:8501` in your browser.
2. Log in with your dashboard password.
3. Go to the **⚙️ System Status** tab.
4. Paste the new access token in the **Update Dhan Access Token** field.
5. Click **🔄 Update Token**.
6. The engine picks up the new token automatically before the next trading session.

### Option B: Edit .env and restart

```bash
cd ~/orb-trader
nano .env
# Update DHAN_ACCESS_TOKEN=new_token_here

docker compose restart
```

### Option C: Automate token retrieval (advanced)

If Dhan provides an API to generate tokens programmatically, you can add a cron job:

```bash
crontab -e
# Add:
0 9 * * 1-5 cd ~/orb-trader && python3 refresh_token.py
```

---

## 15. Troubleshooting

### Container won't start

```bash
# Check build logs
docker compose build --no-cache 2>&1 | tail -50

# Check container logs
docker compose logs --tail 200

# Inspect container
docker inspect orb-trading-bot
```

### "Permission denied" on data directory

```bash
sudo chown -R 1000:1000 data/ logs/
```

### Port already in use

```bash
# Find what's using the port
sudo lsof -i :8000
sudo lsof -i :8501

# Kill the process or change ports in docker-compose.yml
```

### SQLite "database is locked"

This can happen if multiple processes write simultaneously. The system uses WAL mode to minimise this. If it persists:

```bash
# Stop the container
docker compose down

# Check database integrity
sqlite3 data/trades.db "PRAGMA integrity_check;"

# Restart
docker compose up -d
```

### Out of disk space

```bash
# Check disk usage
df -h

# Clean Docker cache
docker system prune -af

# Check data directory
du -sh data/*
```

### Telegram notifications not working

1. Verify bot token: `curl https://api.telegram.org/bot<TOKEN>/getMe`
2. Verify chat ID: `curl https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Ensure the bot is added to the group/channel.

### Market feed disconnects frequently

- Check internet connectivity: `ping -c 5 api.dhan.co`
- Check if token is valid (see System Status tab in dashboard).
- The system auto-reconnects, but check logs for repeated `WebSocket closed` errors.

### ARM architecture issues

The Oracle Always Free VM uses ARM (aarch64). Most Python packages work fine, but if you encounter build errors:

```bash
# Install build dependencies
sudo apt install -y build-essential libffi-dev

# Rebuild
docker compose build --no-cache
```

### Dashboard shows "No data"

- Ensure the main engine has run at least one trading session.
- Check that `data/trades.db` exists and has data:

```bash
sqlite3 data/trades.db "SELECT COUNT(*) FROM system_events;"
```

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────┐
│                  Oracle Cloud VM                      │
│              (VM.Standard.A1.Flex, ARM)               │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │           Docker Container                     │  │
│  │                                                │  │
│  │  ┌──────────┐  ┌──────────┐  ┌─────────────┐ │  │
│  │  │ main.py  │  │ webhook  │  │  dashboard   │ │  │
│  │  │ Strategy │  │ FastAPI  │  │  Streamlit   │ │  │
│  │  │ Engine   │  │ :8000    │  │  :8501       │ │  │
│  │  └────┬─────┘  └──────────┘  └──────┬──────┘ │  │
│  │       │                              │        │  │
│  │       ▼                              ▼        │  │
│  │  ┌────────────────────────────────────────┐   │  │
│  │  │         SQLite (data/trades.db)        │   │  │
│  │  └────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  Volumes: ./data  ./logs  ./nifty50.json             │
└──────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
   ┌──────────┐        ┌──────────┐
   │ Dhan API │        │ Telegram │
   │ & Feed   │        │   Bot    │
   └──────────┘        └──────────┘
```

---

## Quick Reference

| Action | Command |
|--------|---------|
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| Restart | `docker compose restart` |
| View logs | `docker compose logs -f --tail 100` |
| Rebuild | `docker compose up -d --build` |
| Shell into container | `docker exec -it orb-trading-bot bash` |
| Check health | `curl http://localhost:8000/health` |
| Check DB | `sqlite3 data/trades.db ".tables"` |

---

*Last updated: May 2026*
