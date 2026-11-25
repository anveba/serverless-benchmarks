# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""Public SQL storage configuration. Generated passwords stay in private state."""
from dataclasses import asdict, dataclass
from pathlib import Path
import re

POSTGRES_IMAGE = "postgres:16.16-bookworm@sha256:a49f7438ba03589f9b1ca062f2da82e899b48ea1dc50d2449fa09a70be83ebca"


@dataclass
class PostgreSQLConfig:
    type: str = "postgresql"
    provider: str = "docker"
    state_dir: str = "results/postgresql-storage"
    image: str = POSTGRES_IMAGE
    cpus: int = 2
    memory_mb: int = 4096
    mapped_port: int = 9013
    bind_host: str = "127.0.0.1"
    external_host: str = ""
    host: str = ""
    instance_id: str = ""
    storage_id: str = ""
    data_volume: str = ""
    remove_containers: bool = False
    startup_timeout: int = 1200
    region: str = "us-east-1"
    subnet_id: str = ""
    ami_id: str = ""
    instance_type: str = "m7i-flex.large"
    azure_subscription_id: str = ""
    azure_vm_size: str = "Standard_D2s_v5"
    azure_vm_image: str = "Canonical:ubuntu-24_04-lts:server:latest"
    azure_disk_sku: str = "StandardSSD_LRS"
    azure_vnet_cidr: str = "10.73.0.0/16"

    @classmethod
    def deserialize(cls, data):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown PostgreSQL storage configuration fields")
        ret = cls(**data)
        if ret.type != "postgresql" or ret.provider not in ("docker", "aws", "azure"):
            raise ValueError(
                "SQL storage supports PostgreSQL with provider docker, aws or azure"
            )
        if (
            type(ret.cpus) is not int
            or ret.cpus < 1
            or type(ret.memory_mb) is not int
            or ret.memory_mb < 1024
        ):
            raise ValueError("PostgreSQL requires positive CPUs and at least 1024 MiB")
        if type(ret.mapped_port) is not int or not 1 <= ret.mapped_port <= 65535:
            raise ValueError("Invalid PostgreSQL mapped port")
        if type(ret.startup_timeout) is not int or ret.startup_timeout < 1:
            raise ValueError("startup_timeout must be positive seconds")
        if not isinstance(ret.state_dir, str) or not ret.state_dir:
            raise ValueError("A SQL storage state_dir is required")
        ret.state_dir = str(Path(ret.state_dir).expanduser().resolve())
        if not re.fullmatch(
            r"[a-zA-Z0-9:/@._-]+", ret.image
        ) or not ret.image.startswith("postgres:16."):
            raise ValueError(
                "Use a pinned PostgreSQL 16 image reference without whitespace"
            )
        if ret.storage_id and not re.fullmatch(r"[a-f0-9]{12}", ret.storage_id):
            raise ValueError("Invalid SQL storage_id")
        if ret.provider == "aws" and (
            not ret.subnet_id.startswith("subnet-") or not ret.ami_id.startswith("ami-")
        ):
            raise ValueError(
                "AWS SQL storage requires subnet_id and ami_id from preflight"
            )
        if ret.provider == "azure":
            import ipaddress
            import uuid
            try:
                ret.azure_subscription_id = str(uuid.UUID(ret.azure_subscription_id))
                network = ipaddress.ip_network(ret.azure_vnet_cidr, strict=True)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Azure SQL storage requires a subscription UUID and valid IPv4 VNet CIDR") from None
            private_ranges = [ipaddress.ip_network(value) for value in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
            if network.version != 4 or not 16 <= network.prefixlen <= 22 or not any(network.subnet_of(n) for n in private_ranges if network.version == n.version):
                raise ValueError("Azure SQL VNet must be a private IPv4 /16 through /22")
            if not re.fullmatch(r"[a-z][a-z0-9]+", ret.region):
                raise ValueError("Set an Azure region, for example swedencentral")
            if not re.fullmatch(r"Standard_[A-Za-z0-9_]+", ret.azure_vm_size):
                raise ValueError("Invalid Azure VM size")
            if not re.fullmatch(r"Canonical:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[A-Za-z0-9_.-]+", ret.azure_vm_image):
                raise ValueError("Use a Canonical Ubuntu image URN")
            if ret.azure_disk_sku not in ("StandardSSD_LRS", "Premium_LRS"):
                raise ValueError("Unsupported Azure PostgreSQL disk SKU")
        return ret

    def serialize(self):
        # No password, CA private key or AWS access key belongs in this object.
        return asdict(self)


def sql_config(section):
    if (
        not isinstance(section, dict)
        or section.get("type") != "postgresql"
        or set(section) != {"type", "postgresql"}
    ):
        raise ValueError(
            "SQL storage configuration requires type=postgresql and postgresql settings"
        )
    return PostgreSQLConfig.deserialize(section["postgresql"])
