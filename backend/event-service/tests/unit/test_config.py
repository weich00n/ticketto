from app.config import Settings

DB_PARTS = {
    "DB_HOST": "db.example.internal",
    "DB_PORT": "3307",
    "DB_NAME": "db_event",
    "DB_USER": "event_service",
    "DB_PASSWORD": "p@ss:word/with%specials",
}


def test_database_url_is_built_from_parts_when_no_url_is_set(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name, value in DB_PARTS.items():
        monkeypatch.setenv(name, value)
    url = Settings.from_environment().database_url
    assert url.host == "db.example.internal"
    assert url.port == 3307
    assert url.database == "db_event"
    assert url.username == "event_service"
    assert url.password == "p@ss:word/with%specials"


def test_database_url_wins_when_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost:3307/event_service")
    monkeypatch.setenv("DB_HOST", "ignored")
    assert Settings.from_environment().database_url.host == "localhost"


def test_mysql_connections_use_utc_and_optional_tls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost/event_service")
    monkeypatch.delenv("DB_SSL_CA", raising=False)
    args = Settings.from_environment().database_connect_args()
    assert args == {"init_command": "SET time_zone = '+00:00'"}

    monkeypatch.setenv("DB_SSL_CA", "/etc/ssl/rds/global-bundle.pem")
    args = Settings.from_environment().database_connect_args()
    assert args["ssl"] == {"ca": "/etc/ssl/rds/global-bundle.pem"}


def test_sqlite_gets_no_mysql_connect_args():
    settings = Settings(database_url="sqlite+pysqlite:///:memory:", rabbitmq_url="amqp://x")
    assert not settings.database_connect_args()
