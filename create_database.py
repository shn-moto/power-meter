from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from app.storage import apply_migrations
from config import load_app_config


def build_admin_url(database_url: str) -> tuple[str, str]:
    parsed = urlsplit(database_url)
    database_name = parsed.path.lstrip("/") or "home_power_meter"
    admin_url = urlunsplit(parsed._replace(path="/postgres"))
    return admin_url, database_name


def main() -> int:
    config = load_app_config()
    admin_url, database_name = build_admin_url(config.database_url)

    with psycopg.connect(admin_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
            exists = cursor.fetchone() is not None
            if not exists:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
                print(f"Created database {database_name}")
            else:
                print(f"Database {database_name} already exists")

    apply_migrations(config.database_url)
    print("Database schema initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
