# Google Console Deployment — Asim Live on Agent Gold

Deploys the Asim live desk (API + frontend) to both Compute Engine VMs in `agent-gold-507217`.

## Security (applied)

| Layer | What it does |
|---|---|
| **GCP firewall** | Port **443** open only from your IP (update in `deploy.ps1` if it changes) |
| **HTTPS (self-signed)** | Nginx terminates TLS; traffic is encrypted |
| **Login page** | Session cookie auth with **Remember me** |
| **App binding** | FastAPI listens on `127.0.0.1:8080` only (not public) |
| **SSH tunnel** | Optional — access via `localhost` without exposing the VM |

Browser will still warn about the **self-signed certificate** — click Advanced → Proceed. That is expected without a domain.

## Instances

| Name | URL | Login |
|---|---|---|
| hamzatestserver01 | https://35.232.76.12/live | **hamza** / 123451 |
| instance-20260831-171822 | https://35.223.235.204/live | **ali** / 123451 |

MT5 bridge endpoints (`/api/broker/*`) stay open for the bridge — still protected by the IP firewall.

## Deploy

```powershell
cd "Google Console Deployment"
.\deploy.ps1
```

Update `$AllowedSourceIp` in `deploy.ps1` if your public IP changes.

## SSH tunnel (extra private access)

```powershell
.\ssh-tunnel.ps1 -Instance hamzatestserver01
# Open https://localhost:8443/live
```

## Bridge

```env
ASIM_LAB_URL=https://35.232.76.12
```

Python bridge may need `verify=False` for self-signed HTTPS, or use the tunnel and `http://127.0.0.1:8443` via local port forward.

## Engine profile (Goldm backtest reference)

- Trailing stop: **0.25 pt every candle**
- Entry bar: **defer** (no stop test, no trail on the fill candle)
- Stop fill: **slipped SL**, not a worse gap open
- Stop loss: **enabled**
- XAUUSDm M15 lab (HA+raw): **+$18,528** net, PF **14.44**, max loss/trade **−$1.00**
