# Linux deployment (192.168.1.107)

The stack is two containers in `docker-compose.yml`:

- `db` — TimescaleDB (PostgreSQL 16 + Timescale extension), data on a named volume `timescale_data`.
- `power-meter` — the app, talks to `db` over the internal `power-meter` network.

The app container is published to `127.0.0.1:8484` only and exposed to the internet through Cloudflare Tunnel. The DB container has no published ports.

## First-time install

Connect as the dedicated `powermeter` user.

```bash
ssh powermeter@192.168.1.107
git clone https://github.com/shn-moto/power-meter.git
cd power-meter
cp .env.example .env
# Edit .env — see "Environment" below
docker compose up -d --build
docker compose logs -f power-meter
```

On startup the app runs all pending migrations from `app/migrations/*.sql` against the database. The `schema_migrations` table tracks applied versions.

Smoke test:

```bash
curl http://127.0.0.1:8484/health
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version FROM schema_migrations ORDER BY version;"
```

## Updates

```bash
./deploy/server-update.sh
```

The script does `git pull` and `docker compose up -d --build`. Migrations run automatically when the new app container starts.

## Migrations

Migrations live in [app/migrations/](../app/migrations/) and are applied in lexical order:

- `001_init_schema.sql` — base tables (`devices`, `device_connections`, `device_capabilities`, `samples`, `device_events`, `device_cloud_artifacts`).
- `002_timescaledb.sql` — `CREATE EXTENSION timescaledb` + `samples` becomes a hypertable (7-day chunks).
- `003_continuous_aggregates.sql` — continuous aggregates `samples_hourly` → `samples_daily` → `samples_monthly` with refresh policies. Real-time aggregation is on by default, so the dashboard reads include the current open bucket.
- `004_compression_retention.sql` — compress chunks older than 14 days (segmentby `device_id`); retention policy drops raw `samples` older than 1 year. Continuous aggregates keep their data forever.

To add a new migration, drop a `005_<name>.sql` file. Each file is executed in autocommit; if it fails partway, fix the SQL and rerun — only files whose name is in `schema_migrations` are skipped.

## Environment

Add to `.env` on the server:

```
DATABASE_URL=postgresql://power_meter:<password>@db:5432/home_power_meter

POSTGRES_DB=home_power_meter
POSTGRES_USER=power_meter
POSTGRES_PASSWORD=<password>

HOME_NAME=Shunkov Power Hub
APP_TIMEZONE=Europe/Warsaw
ENERGY_TARIFF_PER_KWH=1.12
POLL_INTERVAL_SECONDS=1
SAMPLE_WRITE_INTERVAL_SECONDS=5
```

`DATABASE_URL` must use host `db` (the compose service name) so the app reaches Timescale over the internal network. `POSTGRES_*` are read by the `db` container to bootstrap the cluster on first run.

## Backups

Single command from the host (the `db` container exposes nothing outside the compose network):

```bash
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > backup-$(date +%F).dump
```

Restore:

```bash
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < backup.dump
```

## Cloudflare tunnel

```bash
ssh powermeter@192.168.1.107
cd ~/power-meter
export CLOUDFLARE_TUNNEL_TOKEN='paste-your-token-here'
./deploy/install-cloudflared-service.sh
```

Tunnel routes `power.shunkov.org` to `http://localhost:8484`. The `cloudflared` container runs with `--network host`, so `localhost:8484` resolves to the published `power-meter` port on the host.
