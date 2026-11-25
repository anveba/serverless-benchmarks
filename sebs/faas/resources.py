# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""System resource management for FaaS platforms.

This module provides the abstract base class for managing system-level resources
across different serverless platforms. It coordinates access to storage services,
NoSQL databases, and other cloud resources needed for benchmark execution.

Each platform implementation (AWS, Azure, GCP, Local, etc.) provides concrete
implementations that handle platform-specific resource management while
following the common interface defined here.
"""

from abc import abstractmethod, ABC
from typing import Optional

import docker

from sebs.cache import Cache
from sebs.faas.config import Config
from sebs.faas.storage import PersistentStorage
from sebs.faas.nosql import NoSQLStorage
from sebs.utils import LoggingBase


class SystemResources(ABC, LoggingBase):
    """Abstract base class for system-level resource management.

    This class provides a common interface for managing cloud resources needed
    by benchmark functions across different serverless platforms. It handles the
    provisioning and access to storage services, NoSQL databases, and other
    platform-specific resources.

    Attributes:
        _config: Platform configuration containing credentials and settings
        _cache_client: Cache client for storing resource configurations
        _docker_client: Docker client for container-based resource management
    """

    def __init__(
        self, config: Config, cache_client: Cache, docker_client: docker.client.DockerClient
    ) -> None:
        """Initialize the system resources manager.

        Args:
            config: Platform configuration with credentials and settings
            cache_client: Cache client for configuration persistence
            docker_client: Docker client for container management
        """
        super().__init__()

        self._config = config
        self._cache_client = cache_client
        self._docker_client = docker_client

    @abstractmethod
    def get_storage(self, replace_existing: Optional[bool] = None) -> PersistentStorage:
        """Get or create a persistent storage instance.

        Provides access to object storage services (S3, Azure Blob, GCS, MinIO)
        for storing benchmark input data, function packages, and results. The
        storage instance may be a cloud service or a locally deployed container.

        Args:
            replace_existing: Whether to replace existing benchmark data.
                             If None, uses the default behavior for the platform.

        Returns:
            PersistentStorage: Configured storage instance ready for use

        Raises:
            RuntimeError: If storage service cannot be provisioned or accessed
        """
        pass

    @abstractmethod
    def get_nosql_storage(self) -> NoSQLStorage:
        """Get or create a NoSQL database storage instance.

        Provides access to NoSQL database services (DynamoDB, CosmosDB,
        Datastore, ScyllaDB) for benchmarks that require structured data
        storage with key-value or document-based operations.
        The storage instance may be a cloud service or a locally deployed container.

        Returns:
            NoSQLStorage: Configured NoSQL storage instance ready for use

        Raises:
            RuntimeError: If NoSQL service cannot be provisioned or accessed
        """
        pass

    def get_sql_storage(self):
        """Attach the SQL service supplied by --storage-configuration; never allocate implicitly."""
        if getattr(self, '_sql_storage', None) is None:
            from sebs.storage.postgresql import PostgreSQL
            config = self._config.resources.sql_storage_config
            if config is None:
                raise RuntimeError("SQL storage is missing. Run 'sebs storage start sql' and pass --storage-configuration.")
            if config.provider in ('aws','azure') and self._config._name != config.provider:
                raise ValueError(config.provider+' PostgreSQL storage requires its matching FaaS deployment')
            if config.provider == 'azure':
                if config.region != self._config.region:
                    raise ValueError('Azure PostgreSQL storage and Functions must use the same region')
                if config.azure_subscription_id.lower() != self._config.credentials.subscription_id.lower():
                    raise ValueError('Azure PostgreSQL storage and Functions subscriptions differ')
            self._sql_storage = PostgreSQL.deserialize(config, self._cache_client,
                self._config.resources, self._docker_client, aws_session=getattr(self,'_session',None))
            self._sql_storage.logging_handlers = self._logging_handlers
        return self._sql_storage
