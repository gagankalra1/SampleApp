"""A small key-value repository with two backends.

``memory`` keeps items in a dict, which is what ``docker compose`` and the unit
tests use. ``dynamodb`` talks to a single-table-per-service DynamoDB layout
provisioned by CloudFormation.

The interface is deliberately narrow — get, put, delete, query by partition,
atomic numeric update, conditional put — because that is all the services need,
and a narrow interface is one that both backends can honour identically.

Floats: DynamoDB has no float type, so money is stored as ``Decimal``.
:func:`to_dynamo` and :func:`from_dynamo` convert at the boundary, and callers
only ever see ``float``.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Any, Iterable

from .config import StorageConfig
from .errors import Conflict, NotFound

LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Decimal <-> float conversion
# --------------------------------------------------------------------------
def to_dynamo(value: Any) -> Any:
    """Recursively make a value safe to hand to DynamoDB."""
    if isinstance(value, float):
        # str() first: Decimal(0.1) carries binary float noise, Decimal("0.1")
        # does not.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    if isinstance(value, set):
        return {to_dynamo(v) for v in value}
    return value


def from_dynamo(value: Any) -> Any:
    """Recursively convert a DynamoDB item back into plain Python types."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


class Repository:
    """Backend-agnostic persistence for one service's items."""

    def __init__(self, config: StorageConfig, key_name: str, sort_key_name: str | None = None):
        self.config = config
        self.key_name = key_name
        self.sort_key_name = sort_key_name
        self._backend: _Backend
        if config.backend == "dynamodb":
            self._backend = _DynamoBackend(config, key_name, sort_key_name)
        else:
            self._backend = _MemoryBackend(key_name, sort_key_name)
        LOG.info(
            "repository ready backend=%s table=%s key=%s",
            config.backend,
            config.table_name or "<in-memory>",
            key_name,
        )

    # -- reads -------------------------------------------------------------
    def get(self, key: str, sort_key: str | None = None) -> dict[str, Any] | None:
        return self._backend.get(key, sort_key)

    def require(self, key: str, sort_key: str | None = None) -> dict[str, Any]:
        item = self.get(key, sort_key)
        if item is None:
            raise NotFound(f"No record found for {self.key_name}={key}")
        return item

    def query_partition(self, key: str, limit: int = 100) -> list[dict[str, Any]]:
        """All items sharing a partition key, for composite-key tables."""
        return self._backend.query_partition(key, limit)

    def query_index(
        self, index_name: str, key_name: str, key_value: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Query a global secondary index, e.g. orders by customer."""
        return self._backend.query_index(index_name, key_name, key_value, limit)

    def scan(self, limit: int = 500) -> list[dict[str, Any]]:
        """Full scan. Only ever used for small demo tables and admin views."""
        return self._backend.scan(limit)

    # -- writes ------------------------------------------------------------
    def put(self, item: dict[str, Any]) -> dict[str, Any]:
        if self.key_name not in item:
            raise ValueError(f"item is missing its key attribute {self.key_name!r}")
        self._backend.put(item)
        return item

    def put_if_absent(self, item: dict[str, Any]) -> dict[str, Any]:
        """Create-only write. Raises :class:`Conflict` if the key exists."""
        self._backend.put_if_absent(item)
        return item

    def update_fields(
        self,
        key: str,
        fields: dict[str, Any],
        sort_key: str | None = None,
        *,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set attributes on an existing item.

        ``expected`` makes the write conditional, which is how order status
        transitions avoid two concurrent requests both "winning" (for example
        two cancel requests both refunding the same payment).
        """
        return self._backend.update_fields(key, fields, sort_key, expected)

    def add_to_number(
        self, key: str, field: str, delta: int, sort_key: str | None = None, *, floor: int | None = None
    ) -> dict[str, Any]:
        """Atomically add ``delta`` to a numeric attribute.

        With ``floor`` set, the update only applies while the result stays at or
        above that floor. This is the mechanism that stops inventory going
        negative when several checkouts race for the last unit — the database
        arbitrates, not the application.
        """
        return self._backend.add_to_number(key, field, delta, sort_key, floor)

    def delete(self, key: str, sort_key: str | None = None) -> None:
        self._backend.delete(key, sort_key)

    def health(self) -> bool:
        return self._backend.health()


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------
class _Backend:
    def get(self, key, sort_key):  # pragma: no cover - interface
        raise NotImplementedError

    def query_partition(self, key, limit):  # pragma: no cover
        raise NotImplementedError

    def query_index(self, index_name, key_name, key_value, limit):  # pragma: no cover
        raise NotImplementedError

    def scan(self, limit):  # pragma: no cover
        raise NotImplementedError

    def put(self, item):  # pragma: no cover
        raise NotImplementedError

    def put_if_absent(self, item):  # pragma: no cover
        raise NotImplementedError

    def update_fields(self, key, fields, sort_key, expected):  # pragma: no cover
        raise NotImplementedError

    def add_to_number(self, key, field, delta, sort_key, floor):  # pragma: no cover
        raise NotImplementedError

    def delete(self, key, sort_key):  # pragma: no cover
        raise NotImplementedError

    def health(self):  # pragma: no cover
        raise NotImplementedError


class _MemoryBackend(_Backend):
    """Process-local store guarded by a lock.

    State does not survive a restart and is not shared between replicas, so the
    in-memory backend is for a single local process only. The Kubernetes
    deployment always uses DynamoDB.
    """

    def __init__(self, key_name: str, sort_key_name: str | None):
        self.key_name = key_name
        self.sort_key_name = sort_key_name
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _composite(self, key: str, sort_key: str | None) -> tuple[str, str]:
        return (key, sort_key or "")

    def get(self, key, sort_key):
        with self._lock:
            item = self._items.get(self._composite(key, sort_key))
            return dict(item) if item else None

    def query_partition(self, key, limit):
        with self._lock:
            return [
                dict(item)
                for (partition, _), item in self._items.items()
                if partition == key
            ][:limit]

    def query_index(self, index_name, key_name, key_value, limit):
        with self._lock:
            return [
                dict(item)
                for item in self._items.values()
                if item.get(key_name) == key_value
            ][:limit]

    def scan(self, limit):
        with self._lock:
            return [dict(item) for item in list(self._items.values())[:limit]]

    def put(self, item):
        with self._lock:
            composite = self._composite(
                item[self.key_name],
                item.get(self.sort_key_name) if self.sort_key_name else None,
            )
            self._items[composite] = dict(item)

    def put_if_absent(self, item):
        with self._lock:
            composite = self._composite(
                item[self.key_name],
                item.get(self.sort_key_name) if self.sort_key_name else None,
            )
            if composite in self._items:
                raise Conflict(f"Record already exists for {self.key_name}={composite[0]}")
            self._items[composite] = dict(item)

    def update_fields(self, key, fields, sort_key, expected):
        with self._lock:
            composite = self._composite(key, sort_key)
            item = self._items.get(composite)
            if item is None:
                raise NotFound(f"No record found for {self.key_name}={key}")
            if expected:
                for field, value in expected.items():
                    if item.get(field) != value:
                        raise Conflict(
                            f"Record changed concurrently: expected {field}={value!r}, "
                            f"found {item.get(field)!r}"
                        )
            item.update(fields)
            self._items[composite] = item
            return dict(item)

    def add_to_number(self, key, field, delta, sort_key, floor):
        with self._lock:
            composite = self._composite(key, sort_key)
            item = self._items.get(composite)
            if item is None:
                raise NotFound(f"No record found for {self.key_name}={key}")
            current = int(item.get(field, 0))
            updated = current + delta
            if floor is not None and updated < floor:
                raise Conflict(
                    f"{field} would fall below {floor} (current={current}, delta={delta})"
                )
            item[field] = updated
            self._items[composite] = item
            return dict(item)

    def delete(self, key, sort_key):
        with self._lock:
            self._items.pop(self._composite(key, sort_key), None)

    def health(self):
        return True


class _DynamoBackend(_Backend):
    def __init__(self, config: StorageConfig, key_name: str, sort_key_name: str | None):
        import boto3

        self.key_name = key_name
        self.sort_key_name = sort_key_name
        self.table_name = config.table_name
        client_kwargs: dict[str, Any] = {"region_name": config.region}
        if config.endpoint_url:
            # Only set for DynamoDB Local during development.
            client_kwargs["endpoint_url"] = config.endpoint_url
        self._resource = boto3.resource("dynamodb", **client_kwargs)
        self._table = self._resource.Table(config.table_name)

    def _key(self, key: str, sort_key: str | None) -> dict[str, Any]:
        composite = {self.key_name: key}
        if self.sort_key_name and sort_key is not None:
            composite[self.sort_key_name] = sort_key
        return composite

    def get(self, key, sort_key):
        response = self._table.get_item(Key=self._key(key, sort_key))
        item = response.get("Item")
        return from_dynamo(item) if item else None

    def query_partition(self, key, limit):
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            KeyConditionExpression=Key(self.key_name).eq(key), Limit=limit
        )
        return [from_dynamo(item) for item in response.get("Items", [])]

    def query_index(self, index_name, key_name, key_value, limit):
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            IndexName=index_name,
            KeyConditionExpression=Key(key_name).eq(key_value),
            Limit=limit,
        )
        return [from_dynamo(item) for item in response.get("Items", [])]

    def scan(self, limit):
        response = self._table.scan(Limit=limit)
        return [from_dynamo(item) for item in response.get("Items", [])]

    def put(self, item):
        self._table.put_item(Item=to_dynamo(item))

    def put_if_absent(self, item):
        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item=to_dynamo(item),
                ConditionExpression=f"attribute_not_exists({self.key_name})",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise Conflict(
                    f"Record already exists for {self.key_name}={item[self.key_name]}"
                ) from exc
            raise

    def update_fields(self, key, fields, sort_key, expected):
        from botocore.exceptions import ClientError

        # Attribute names are placeholdered because several of them ("status",
        # "total") collide with DynamoDB reserved words.
        set_clauses = []
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        for index, (field, value) in enumerate(fields.items()):
            names[f"#f{index}"] = field
            values[f":v{index}"] = to_dynamo(value)
            set_clauses.append(f"#f{index} = :v{index}")

        condition_parts = [f"attribute_exists({self.key_name})"]
        if expected:
            for index, (field, value) in enumerate(expected.items()):
                names[f"#e{index}"] = field
                values[f":e{index}"] = to_dynamo(value)
                condition_parts.append(f"#e{index} = :e{index}")

        try:
            response = self._table.update_item(
                Key=self._key(key, sort_key),
                UpdateExpression="SET " + ", ".join(set_clauses),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=" AND ".join(condition_parts),
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Either the item vanished or an expected value did not match.
                # Distinguish the two so the caller can return 404 vs 409.
                if self.get(key, sort_key) is None:
                    raise NotFound(f"No record found for {self.key_name}={key}") from exc
                raise Conflict(
                    "Record changed concurrently; re-read it and retry"
                ) from exc
            raise
        return from_dynamo(response["Attributes"])

    def add_to_number(self, key, field, delta, sort_key, floor):
        from botocore.exceptions import ClientError

        names = {"#f": field}
        values = {":delta": delta}
        condition_parts = [f"attribute_exists({self.key_name})"]
        if floor is not None:
            # Guard on the pre-update value: available + delta >= floor becomes
            # available >= floor - delta, which DynamoDB can evaluate directly.
            values[":min"] = floor - delta
            condition_parts.append("#f >= :min")

        try:
            response = self._table.update_item(
                Key=self._key(key, sort_key),
                UpdateExpression="SET #f = #f + :delta",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=" AND ".join(condition_parts),
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                if self.get(key, sort_key) is None:
                    raise NotFound(f"No record found for {self.key_name}={key}") from exc
                raise Conflict(
                    f"{field} would fall below {floor} for {self.key_name}={key}"
                ) from exc
            raise
        return from_dynamo(response["Attributes"])

    def delete(self, key, sort_key):
        self._table.delete_item(Key=self._key(key, sort_key))

    def health(self):
        try:
            self._table.load()
            return True
        except Exception:
            LOG.warning("dynamodb health check failed for table=%s", self.table_name)
            return False


def seed_if_empty(repository: Repository, items: Iterable[dict[str, Any]]) -> int:
    """Insert ``items`` that are not already present. Returns the count written.

    Runs on startup so a fresh workshop account gets a populated store, and is
    safe to run on every pod start because each write is create-only.
    """
    written = 0
    for item in items:
        try:
            repository.put_if_absent(item)
            written += 1
        except Conflict:
            continue
    if written:
        LOG.info("seeded %d records into %s", written, repository.config.table_name or "memory")
    return written
