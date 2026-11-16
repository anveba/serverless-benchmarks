"""Compatibility import; generic SQL loading is owned by SeBS storage."""
from sebs.storage.postgresql_loader import (
    file_sha256,
    load_dataset,
    main,
    postgres_type,
)

if __name__ == "__main__":
    main()
