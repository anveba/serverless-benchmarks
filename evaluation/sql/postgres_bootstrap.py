"""Compatibility import; PostgreSQL bootstrap is owned by SeBS storage."""
from sebs.storage.aws_postgresql_bootstrap import (
    DRIVER_VERSION,
    POSTGRES_IMAGE,
    user_data,
)
