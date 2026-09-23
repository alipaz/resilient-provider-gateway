from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def session_factory(database_url: str):
    return sessionmaker(bind=create_engine(database_url), expire_on_commit=False)
