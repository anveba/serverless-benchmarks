# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""PostgreSQL storage with Docker, AWS and Azure lifecycle/transport providers."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time
import uuid

from sebs.faas.sql import SQLStorage
from sebs.storage.sql_config import PostgreSQLConfig
from sebs.storage.postgresql_loader import file_sha256, load_dataset


def protected_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def build_dataset(storage_id, benchmark, size, tables, metadata):
    if not re.fullmatch(r"[0-9]+\.[a-z0-9-]+", benchmark) or not re.fullmatch(
        r"[a-zA-Z0-9_.-]+", size
    ):
        raise ValueError("Invalid benchmark/input name for SQL storage")
    identity = hashlib.sha256((storage_id + benchmark + size).encode()).hexdigest()[:20]
    namespace = "sebs_" + benchmark.split(".")[0] + "_" + identity
    prepared = {}
    for table, spec in tables.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", table) or not spec["files"]:
            raise ValueError("Invalid SQL dataset table")
        prepared[table] = {
            "primary_key": list(spec.get("primary_key", [])),
            "files": [
                {"path": str(Path(path).resolve()), "sha256": file_sha256(path)}
                for path in spec["files"]
            ],
        }
    # File contents, keys and semantic metadata define the fingerprint, not local paths.
    content = {
        "tables": {
            table: {
                "primary_key": value["primary_key"],
                "sha256": [f["sha256"] for f in value["files"]],
            }
            for table, value in prepared.items()
        },
        "metadata": metadata,
    }
    fingerprint = hashlib.sha256(
        json.dumps(content, sort_keys=True).encode()
    ).hexdigest()
    return {
        "namespace": namespace,
        "fingerprint": fingerprint,
        "tables": prepared,
        "metadata": metadata,
    }


class PostgreSQL(SQLStorage):
    @staticmethod
    def deployment_name():
        return "postgresql"

    @staticmethod
    def typename():
        return "Storage.PostgreSQL"

    def __init__(
        self, docker_client, cache_client, config, resources=None, aws_session=None
    ):
        super().__init__()
        self.config = config
        self.cache_client = cache_client
        self.resources = resources
        self.docker = docker_client
        self.folder = Path(config.state_dir)
        self.backend = None
        public = self.folder / "storage.json"
        if public.exists():
            saved = PostgreSQLConfig.deserialize(json.loads(public.read_text()))
            for key in (
                "provider",
                "image",
                "cpus",
                "memory_mb",
                "mapped_port",
                "bind_host",
                "region",
                "subnet_id",
                "ami_id",
                "instance_type",
                "azure_subscription_id",
                "azure_vm_size",
                "azure_vm_image",
                "azure_disk_sku",
                "azure_vnet_cidr",
            ):
                if getattr(saved, key) != getattr(config, key):
                    raise ValueError(
                        "Existing SQL storage differs at "
                        + key
                        + "; use another state_dir"
                    )
            # Explicit advertised addresses can change without changing ownership.
            for key in ("external_host",):
                if getattr(config, key):
                    setattr(saved, key, getattr(config, key))
            self.config = saved
        if self.config.provider == "aws":
            from sebs.storage.aws_postgresql import AWSPostgreSQL

            self.backend = AWSPostgreSQL(self.config, aws_session)
        elif self.config.provider == "azure":
            from sebs.storage.azure_postgresql import AzurePostgreSQL

            self.backend = AzurePostgreSQL(self.config)

    @classmethod
    def deserialize(
        cls,
        config,
        cache_client=None,
        resources=None,
        docker_client=None,
        aws_session=None,
    ):
        if config.provider == "docker" and docker_client is None:
            import docker

            docker_client = docker.from_env()
        return cls(docker_client, cache_client, config, resources, aws_session)

    def save(self):
        protected_json(self.folder / "storage.json", self.config.serialize())
        if self.cache_client is not None and self.resources is not None:
            self.resources._sql_storage_config = self.config
            self.resources.update_cache(self.cache_client)

    def private(self):
        path = self.folder / "private.json"
        if not path.exists():
            raise RuntimeError(
                "SQL storage credentials are missing; start storage before preparing input"
            )
        return json.loads(path.read_text())

    def admin(self):
        state = self.private()
        return {
            "host": "127.0.0.1",
            "port": self.config.mapped_port,
            "dbname": "sebs",
            "user": "postgres",
            "password": state["admin_password"],
            "sslmode": "disable",
            "connect_timeout": 5,
        }

    def container(self):
        container = self.docker.containers.get(self.config.instance_id)
        container.reload()
        if container.labels.get("sebs.sql.storage") != self.config.storage_id:
            raise RuntimeError(
                "Container does not belong to this SQL storage configuration"
            )
        return container

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        if not self.config.storage_id:
            self.config.storage_id = uuid.uuid4().hex[:12]
        self.save()  # Persist ownership before allocating any resource.
        if self.backend is not None:
            self.backend.start()
            self.save()
            return
        try:
            import psycopg
        except ImportError:
            raise RuntimeError(
                'SQL storage requires the host client: pip install -e ".[sql]"'
            ) from None
        secret_path = self.folder / "private.json"
        if not secret_path.exists():
            protected_json(
                secret_path,
                {
                    "admin_password": secrets.token_urlsafe(40),
                    "reader_password": secrets.token_urlsafe(40),
                },
            )
        state = self.private()
        if self.config.instance_id:
            container = self.container()
            if container.status != "running":
                container.start()
        else:
            volume = (
                self.config.data_volume or "sebs-postgresql-" + self.config.storage_id
            )
            self.config.data_volume = volume
            self.save()
            # Do not adopt an arbitrary pre-existing volume with unknown credentials.
            volumes = self.docker.volumes.list(filters={"name": volume})
            exact = [v for v in volumes if v.name == volume]
            if (
                exact
                and (exact[0].attrs.get("Labels") or {}).get("sebs.sql.storage")
                != self.config.storage_id
            ):
                raise ValueError(
                    "Existing Docker volume is not owned by this SQL configuration"
                )
            if not exact:
                self.docker.volumes.create(
                    name=volume, labels={"sebs.sql.storage": self.config.storage_id}
                )
            self.docker.images.pull(self.config.image)
            container = self.docker.containers.run(
                self.config.image,
                detach=True,
                name="sebs-postgresql-" + self.config.storage_id,
                network_mode="bridge",
                labels={"sebs.sql.storage": self.config.storage_id},
                restart_policy={"Name": "unless-stopped"},
                environment={
                    "POSTGRES_DB": "sebs",
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_PASSWORD": state["admin_password"],
                },
                ports={"5432/tcp": (self.config.bind_host, self.config.mapped_port)},
                volumes={volume: {"bind": "/var/lib/postgresql/data", "mode": "rw"}},
                nano_cpus=self.config.cpus * 10**9,
                mem_limit=str(self.config.memory_mb) + "m",
                shm_size="256m",
                command=[
                    "-c",
                    "password_encryption=scram-sha-256",
                    "-c",
                    "shared_buffers="
                    + str(min(1024, self.config.memory_mb // 4))
                    + "MB",
                    "-c",
                    "work_mem=32MB",
                    "-c",
                    "max_connections=32",
                    "-c",
                    "max_parallel_workers_per_gather=1",
                ],
            )
            self.config.instance_id = container.id
            self.save()
        deadline = time.monotonic() + self.config.startup_timeout
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(**self.admin(), autocommit=True) as con:
                    from psycopg import sql

                    if not con.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname='sebs_reader'"
                    ).fetchone():
                        con.execute(
                            sql.SQL("CREATE ROLE sebs_reader LOGIN PASSWORD {}").format(
                                sql.Literal(state["reader_password"])
                            )
                        )
                    con.execute(
                        "ALTER ROLE sebs_reader SET default_transaction_read_only=on"
                    )
                    con.execute("ALTER ROLE sebs_reader SET statement_timeout=240000")
                break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            raise TimeoutError(
                "PostgreSQL not ready; inspect the saved container ID and retry the same configuration"
            )
        container.reload()
        self.config.host = container.attrs["NetworkSettings"].get(
            "IPAddress"
        ) or container.attrs["NetworkSettings"].get("Networks", {}).get(
            "bridge", {}
        ).get(
            "IPAddress", ""
        )
        if not self.config.host:
            raise RuntimeError(
                "Docker did not advertise an internal PostgreSQL address"
            )
        self.save()

    def prepare_dataset(self, benchmark, size, tables, metadata, replace=False):
        if not self.config.storage_id:
            raise RuntimeError("Start SQL storage before preparing a benchmark")
        declared = self._declared_tables.get(benchmark)
        if declared is not None and (set(declared) != set(tables) or any(
                list(tables[name].get('primary_key',[])) != declared[name] for name in declared)):
            raise ValueError('SQL input tables/keys differ from allocate_sql declarations')
        dataset = build_dataset(
            self.config.storage_id, benchmark, size, tables, metadata
        )
        self.logging.info("Preparing SQL namespace " + dataset["namespace"])
        if self.backend is not None:
            report = self.backend.prepare_dataset(dataset, replace)
        else:
            self.container()  # Refuse lost/replaced containers before opening a DB connection.
            report = load_dataset(
                dataset,
                self.admin(),
                lambda item, target: shutil.copyfile(item["path"], target),
                replace=replace,
            )
        protected_json(
            self.folder / "datasets" / (dataset["namespace"] + ".json"), report
        )
        self.save()
        return {
            "namespace": dataset["namespace"],
            "input_fingerprint": dataset["fingerprint"],
        }

    def envs(self, external=True):
        if self.backend is not None:
            return self.backend.envs(external)
        state = self.private()
        host = self.config.external_host if external else self.config.host
        if not host:
            raise RuntimeError(
                "Set the SQL storage external_host for functions outside the Docker bridge"
            )
        return {
            "SEBS_SQL_CONFIG": json.dumps(
                {
                    "type": "postgresql",
                    "host": host,
                    "port": self.config.mapped_port if external else 5432,
                    "dbname": "sebs",
                    "user": "sebs_reader",
                    "password": state["reader_password"],
                    "sslmode": "disable",
                }
            )
        }

    def function_options(self):
        if self.backend is not None:
            return self.backend.function_options()
        return {"Environment": {"Variables": self.envs()}}

    def status(self):
        if self.backend is not None:
            return self.backend.status()
        if not self.config.instance_id:
            return {
                "provider": "docker",
                "state": "stopped" if self.config.data_volume else "not-created",
            }
        container = self.container()
        return {
            "provider": "docker",
            "state": container.status,
            "instance_id": container.id,
            "host": self.config.host,
            "mapped_port": self.config.mapped_port,
            "image": self.config.image,
        }

    def stop(self):
        if self.backend is not None:
            return self.backend.stop()
        container = self.container()
        container.stop()
        if self.config.remove_containers:
            container.remove()
            self.config.instance_id = ""
            self.save()
        return {
            "state": "stopped",
            "data_volume": self.config.data_volume,
            "data_retained": True,
        }

    def remove(self, delete_data=False, dry_run=False):
        if self.backend is not None:
            return self.backend.remove(delete_data, dry_run)
        if dry_run:
            return {
                "instance_id": self.config.instance_id,
                "data_volume": self.config.data_volume,
                "delete_data": delete_data,
            }
        if self.config.instance_id:
            self.container().remove(force=True)
        self.config.instance_id = ""
        if delete_data:
            volume = self.docker.volumes.get(self.config.data_volume)
            if (volume.attrs.get("Labels") or {}).get(
                "sebs.sql.storage"
            ) != self.config.storage_id:
                raise RuntimeError("Refusing to remove an unowned SQL volume")
            volume.remove()
            self.config.data_volume = ""
        self.save()
        return {"state": "removed", "data_retained": not delete_data}

    def serialize(self):
        return self.config.serialize()
