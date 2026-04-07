# Deployment Guide

## VPS Setup
- Provider: Hetzner Cloud (EU) or DigitalOcean (Amsterdam/Frankfurt)
- Instance: CPX11 (2 vCPU, 2GB RAM, ~$5/month)
- OS: Ubuntu 22.04 LTS

## Setup steps
1. Provision VPS with SSH key
2. Install Python 3.11+, uv, sqlite3
3. Clone repo, install deps (`uv sync`)
4. Copy .env with API keys
5. Set up systemd service (see below)
6. Set up WireGuard VPN to Switzerland (live trading only)
7. Set up log rotation

## systemd service (kalbot.service)
```ini
[Unit]
Description=KalBot Trading Bot
After=network.target

[Service]
Type=simple
User=kalbot
WorkingDir=/opt/kalbot
ExecStart=/opt/kalbot/.venv/bin/python -m kalbot.main
Restart=always
RestartSec=5
Environment=KALBOT_ENV=paper

[Install]
WantedBy=multi-user.target
```

## VPN (live trading only)
- Provider: Mullvad or Proton VPN (Swiss exit)
- Config: /etc/wireguard/wg0.conf
- Start: `wg-quick up wg0`
- Bot verifies VPN active before placing real orders

## Legacy Data
DO NOT import old 46 trades for ML training. 82% have corrupted window features (elapsed_s up to 3500s). New system starts fresh. 288 windows/day = 2000+ clean samples in 1 week.
