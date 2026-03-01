# Deploying to VPS (Hetzner CX22)

Paper trading deployment for month-long test run.

## 1. Provision the Server

1. Create a [Hetzner Cloud](https://console.hetzner.cloud/) account
2. Create a **CX22** server:
   - **OS:** Ubuntu 24.04
   - **Region:** Ashburn (us-east)
   - **SSH Key:** Add your public key (`~/.ssh/id_ed25519.pub`)
   - **Name:** `polymarket-paper`
3. Note the IP address

## 2. Initial Server Setup

```bash
ssh root@YOUR_VPS_IP

# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Verify
docker compose version

# Create bot user with Docker access
useradd -m -s /bin/bash -G docker botuser

# Copy SSH key for botuser
mkdir -p /home/botuser/.ssh
cp ~/.ssh/authorized_keys /home/botuser/.ssh/
chown -R botuser:botuser /home/botuser/.ssh
chmod 700 /home/botuser/.ssh
chmod 600 /home/botuser/.ssh/authorized_keys

# Firewall — SSH only
ufw allow OpenSSH
ufw --force enable

# Done as root — switch to botuser for everything else
exit
```

## 3. Deploy the Bot

```bash
ssh botuser@YOUR_VPS_IP

# Clone the repo
git clone YOUR_REPO_URL polymarket-unified
cd polymarket-unified

# Configure environment
cp .env.example .env
nano .env
```

**Critical .env settings to verify:**
```
DRY_RUN=true          # MUST be true for paper trading
LOG_LEVEL=INFO
MM_ENABLED=true
ARB_ENABLED=true
TIMEFRAMES=5m,15m
```

Leave `PRIVATE_KEY` and `PUBLIC_ADDRESS` empty for paper trading — they're only needed for live orders.

```bash
# Build and start
docker compose up -d --build

# Watch startup logs (Ctrl+C to stop watching — bot keeps running)
docker compose logs -f
```

You should see:
- "Settings loaded" with DRY_RUN=true
- Market discovery finding BTC 5m/15m markets
- "WebSocket connected" messages
- Periodic P&L reports every 30 seconds

## 4. Set Up Monitoring

### Watchdog cron (auto-restart if container dies)

```bash
crontab -e
```

Add this line:
```
*/5 * * * * docker inspect --format='{{.State.Running}}' polymarket-unified 2>/dev/null | grep -q true || (cd ~/polymarket-unified && docker compose up -d) >> ~/watchdog.log 2>&1
```

### Daily log export (for P&L analysis)

```bash
crontab -e
```

Add this line:
```
0 0 * * * bash ~/polymarket-unified/scripts/export-logs.sh ~/log-exports
```

## 5. Daily Operations

### Quick status check

```bash
ssh botuser@YOUR_VPS_IP
bash ~/polymarket-unified/scripts/status.sh
```

### View P&L reports

```bash
docker compose -f ~/polymarket-unified/docker-compose.yml logs --no-log-prefix | grep "SIMULATED P&L"
```

### View arbitrage executions

```bash
docker compose -f ~/polymarket-unified/docker-compose.yml logs --no-log-prefix | grep -i "arb"
```

### Export logs for local analysis

```bash
# On VPS
bash ~/polymarket-unified/scripts/export-logs.sh

# On your Mac
scp botuser@YOUR_VPS_IP:~/log-exports/latest_file.jsonl ./
```

## 6. Updating the Bot

### Settings change (no rebuild)

```bash
ssh botuser@YOUR_VPS_IP
cd ~/polymarket-unified
nano .env
docker compose down && docker compose up -d
```

### Code update (requires rebuild)

```bash
ssh botuser@YOUR_VPS_IP
bash ~/polymarket-unified/scripts/deploy.sh
```

## 7. Teardown (after the month)

```bash
ssh botuser@YOUR_VPS_IP

# Export final logs
bash ~/polymarket-unified/scripts/export-logs.sh ~/log-exports

# Download logs to your Mac (from your Mac)
scp -r botuser@YOUR_VPS_IP:~/log-exports ./final-logs/

# Stop the bot
cd ~/polymarket-unified
docker compose down
```

Then delete the Hetzner server from the console.

## Architecture Notes

- **Single process:** Both Market Maker and Arbitrage run in one async Python process
- **Auto-recovery:** WebSocket reconnects with exponential backoff (1s to 30s). Docker restarts the container if the process crashes.
- **In-memory state:** P&L tracking resets on restart. The periodic P&L reports in the logs are your persistent record.
- **Log rotation:** Docker caps logs at 500MB total (50MB x 10 files), auto-rotated.
- **Cost:** ~$3.60/month for the Hetzner CX22.
