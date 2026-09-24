from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def build_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    database_url = (settings or Settings()).DATABASE_URL
    return sessionmaker(bind=create_engine(database_url), expire_on_commit=False)
