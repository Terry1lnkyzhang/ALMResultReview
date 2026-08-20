from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine_options: dict[str, object] = {"pool_pre_ping": True, "pool_recycle": 1800}
if not settings.database_url.startswith("sqlite"):
    # Room for concurrent review threads, the worker cycle and web requests.
    engine_options |= {"pool_size": 10, "max_overflow": 20, "pool_timeout": 30}
engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session