from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


def _engine_options(database_url: str) -> dict:
    options = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
        options.update(
            pool_recycle=300,
            pool_timeout=30,
            connect_args={"connect_timeout": 10},
        )
    return options


engine = create_engine(settings.database_url, **_engine_options(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()