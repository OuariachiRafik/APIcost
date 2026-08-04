"""Declarative base and shared metadata.

Models themselves arrive in P1 (``db/models.py``). What lives here is the
naming convention, which must be in place *before* the first model migration:
Alembic can only autogenerate a reversible ``downgrade`` for a constraint it
can name deterministically (BUILD_SPEC §11.3).
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    metadata = metadata
