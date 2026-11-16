# Copyright 2020-2026 ETH Zurich and the SeBS authors. All rights reserved.
"""One complete analytical SQL query per invocation, on external SQL storage."""
import time

from . import sql
from .queries import QUERIES

client = sql.sql.get_instance()


def handler(event):
    begin = time.perf_counter()
    selected = event.get("queries")
    if (
        not isinstance(selected, list)
        or len(selected) != 1
        or not isinstance(selected[0], str)
        or selected[0] not in QUERIES
    ):
        raise ValueError("Exactly one supported query ID is required")
    query = selected[0]
    response = client.query(
        QUERIES[query],
        event["namespace"],
        event["input_fingerprint"],
        policy=event.get("connection_policy", "fresh"),
        statement_timeout_ms=event.get("statement_timeout_ms", 120000),
    )
    result = {
        "result": {"queries": {query: response.pop("rows")}},
        "measurement": {query: response.pop("measurement")},
        **response,
    }

    result["measurement"][query]["handler_time"] = (time.perf_counter() - begin) * 1e6
    return result
