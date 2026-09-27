from typing import Any

_store: dict[str, Any] = {}


def get(key: str) -> Any | None:
    return _store.get(key)


def set(key: str, value: Any) -> None:
    _store[key] = value


def delete(key: str) -> None:
    _store.pop(key, None)


def clear() -> None:
    _store.clear()
