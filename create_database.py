from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from app.storage import init_db, sync_devices
from config import load_app_config, load_devices


def build_admin_url(database_url: str) -> tuple[str, str]:
    parsed = urlsplit(database_url)
    database_name = parsed.path.lstrip("/") or "home_power_meter"
    admin_url = urlunsplit(parsed._replace(path="/postgres"))
    return admin_url, database_name


def main() -> int:
    config = load_app_config()
    devices = load_devices()
    admin_url, database_name = build_admin_url(config.database_url)

    with psycopg.connect(admin_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
            exists = cursor.fetchone() is not None
            if not exists:
                cursor.execute(sql.SQL("CREATE DATABASE {}") .format(sql.Identifier(database_name)))
                print(f"Created database {database_name}")
            else:
                print(f"Database {database_name} already exists")

    init_db(config)
    sync_devices(config, devices)
    print("Database schema initialized and devices synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())