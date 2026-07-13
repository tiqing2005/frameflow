from __future__ import annotations

from typing import Annotated, Generator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ..config import Settings


def get_session(request: Request) -> Generator[Session, None, None]:
    session = request.app.state.database.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
