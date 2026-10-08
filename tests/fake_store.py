"""Small in-memory Data API boundary for service integration tests."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4


class MemoryStore:
    def __init__(self):
        self.tables = {"domains": [{"id": "22222222-2222-4222-8222-222222222222", "slug": "japan_immigration", "name": "Test"}]}

    def table(self, name):
        return Query(self.tables.setdefault(name, []))


class Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.payload = None
        self.action = "select"
        self.count = None
        self.maximum = 10000

    def select(self, *_args, **kwargs):
        self.count = kwargs.get("count")
        return self

    def eq(self, key, value):
        def matches(row):
            if "->>" in key:
                current = row
                for field in key.replace("->>", "->").split("->"):
                    current = current.get(field) if isinstance(current, dict) else None
                return current == value
            return row.get(key) == value
        self.filters.append(matches)
        return self

    def is_(self, key, value):
        return self.eq(key, None if value == "null" else value)

    def in_(self, key, values):
        self.filters.append(lambda row: row.get(key) in values)
        return self

    def gte(self, key, value):
        self.filters.append(lambda row: row.get(key) is not None and row[key] >= value)
        return self

    def lt(self, key, value):
        self.filters.append(lambda row: row.get(key) is not None and row[key] < value)
        return self

    def limit(self, value):
        self.maximum = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    def insert(self, payload):
        self.action, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.action, self.payload = "update", payload
        return self

    def execute(self):
        if self.action == "insert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            result = []
            for payload in payloads:
                now = datetime.now(timezone.utc).isoformat()
                row = {"id": str(uuid4()), "created_at": now, "updated_at": now, "deleted_at": None, **deepcopy(payload)}
                self.rows.append(row)
                result.append(row)
        else:
            result = [row for row in self.rows if all(test(row) for test in self.filters)][:self.maximum]
            if self.action == "update":
                for row in result:
                    row.update(deepcopy(self.payload))
        return SimpleNamespace(data=deepcopy(result), count=len(result) if self.count else None)
