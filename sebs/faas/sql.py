# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""SQL storage lifecycle used by benchmark input preparation and FaaS adapters."""
from abc import ABC, abstractmethod
import copy

from sebs.utils import LoggingBase


class SQLStorage(ABC, LoggingBase):
    """A separately running SQL service; dataset setup is outside invocation timing."""

    def __init__(self):
        super().__init__()
        self._declared_tables = {}

    def allocate_benchmark(self, benchmark, tables):
        """Record declared tables/primary keys before the input generator loads data."""
        if not isinstance(tables,dict) or any(not isinstance(name,str) or not isinstance(keys,list)
                or any(not isinstance(key,str) for key in keys) for name,keys in tables.items()):
            raise ValueError('allocate_sql must return table names mapped to primary-key column lists')
        self._declared_tables[benchmark] = copy.deepcopy(tables)

    @abstractmethod
    def start(self):
        """Start or reuse the configured storage service."""

    @abstractmethod
    def stop(self):
        """Stop only the service owned by this configuration."""

    @abstractmethod
    def status(self):
        """Return readiness and resource identifiers, without secrets."""

    @abstractmethod
    def prepare_dataset(self, benchmark, size, tables, metadata, replace=False):
        """Load benchmark-owned tables and return their namespace and fingerprint.

        tables maps a table name to local files and primary_key columns. Different
        benchmark/input pairs have distinct namespaces; the backend owns transport,
        bulk loading and cache checks, not the benchmark function.
        """

    @abstractmethod
    def envs(self, external=True):
        """Connection settings for the runtime wrapper; never log the returned values."""

    @abstractmethod
    def serialize(self):
        """Non-secret configuration for reuse and cleanup."""

    @abstractmethod
    def remove(self, delete_data=False, dry_run=False):
        """Remove owned resources, with explicit data deletion and a read-only preview."""
