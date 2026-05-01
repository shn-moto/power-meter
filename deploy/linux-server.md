# Linux deployment

## Repository-based deployment

Clone the repository on the Linux server and keep the production `.env` only on the server.

```bash
git clone git@github.com:shn-moto/power-meter.git
cd power-meter
cp .env.example .env
python create_database.py
docker compose up -d --build
```

For updates after new commits:

```bash
./deploy/server-update.sh
```

## App container

The app is bound to `127.0.0.1:8484` through `docker-compose.yml` so it stays private on the server and is exposed only through Cloudflare Tunnel. The application stores live measurements in PostgreSQL through `DATABASE_URL`.

```bash
python create_database.py
docker compose up -d --build
curl http://127.0.0.1:8484/health
```

## One-time historical sync

Local Tuya polling gives current values only. If Tuya Cloud logs are available, you can import them once into PostgreSQL and then continue operating from the database plus live polling.

```bash
export TUYA_CLOUD_API_REGION=eu
export TUYA_CLOUD_API_KEY=...
export TUYA_CLOUD_API_SECRET=...
python sync_history.py --days 90
```

The sync stores all fetched cloud events in `device_events` and also writes normalized power samples into `samples` whenever the cloud log payload contains the configured power DPS key.

## Cloudflare tunnel

On the Linux host install `cloudflared`, then run the service install using the tunnel token as an environment variable instead of hardcoding it into the repository.

```bash
export CLOUDFLARE_TUNNEL_TOKEN='paste-your-token-here'
./deploy/install-cloudflared-service.sh
```

If your existing tunnel is already configured in Cloudflare to route `power.shunkov.org` to `http://localhost:8484`, no extra ingress file is required.

## Environment

Keep production secrets in `.env` on the server. The repository includes `.env.example` and `devices.catalog.json.example` to show the expected format.