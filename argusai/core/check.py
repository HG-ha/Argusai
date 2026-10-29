"""Check base class and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar, Iterable

from argusai.core.context import Context
from argusai.core.finding import Finding
from argusai.core.target import Target


class Check(ABC):
    """A pluggable security check.

    Subclasses should set the class variables ``id``, ``name`` and
    ``needs_browser`` and implement :meth:`run` as an async generator yielding
    :class:`Finding` instances.
    """

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    needs_browser: ClassVar[bool] = False
    # A user-facing ordering hint — recon should run first so later checks
    # can reuse whatever it discovered (shared context.shared).
    order: ClassVar[int] = 100

    @abstractmethod
    def run(self, target: Target, ctx: Context) -> AsyncIterator[Finding]:
        """Yield findings asynchronously."""
        raise NotImplementedError


class Registry:
    """Simple class-level registry for check types."""

    _checks: dict[str, type[Check]] = {}
    _aliases: dict[str, str] = {
        # Backwards-compatible and model-friendly names. Keep Check.id stable;
        # task params are persisted and older tasks may contain these values.
        "files": "upload",
        "file": "upload",
        "file_upload": "upload",
        "uploads": "upload",
        "active_scanner": "scanner",
        "scan": "scanner",
    }

    @classmethod
    def register(cls, check_cls: type[Check]) -> type[Check]:
        if not check_cls.id:
            raise ValueError(f"{check_cls.__name__} is missing an id")
        cls._checks[check_cls.id] = check_cls
        return check_cls

    @classmethod
    def all(cls) -> list[type[Check]]:
        return sorted(cls._checks.values(), key=lambda c: (c.order, c.id))

    @classmethod
    def get(cls, id: str) -> type[Check] | None:
        return cls._checks.get(cls._aliases.get(id, id))

    @classmethod
    def canonical_ids(cls, ids: Iterable[str] | None) -> list[str]:
        """Return stable check ids after expanding aliases and profiles."""
        if not ids:
            return [c.id for c in cls.all()]
        out: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            check_id = str(raw or "").strip()
            if not check_id:
                continue
            if check_id in {"all", "*"}:
                for check_cls in cls.all():
                    if check_cls.id not in seen:
                        seen.add(check_cls.id)
                        out.append(check_cls.id)
                continue
            check_id = cls._aliases.get(check_id, check_id)
            if check_id not in cls._checks:
                available = ", ".join(sorted(cls._checks))
                aliases = ", ".join(f"{k}->{v}" for k, v in sorted(cls._aliases.items()))
                raise KeyError(f"unknown check: {raw} (available: {available}; aliases: {aliases})")
            if check_id not in seen:
                seen.add(check_id)
                out.append(check_id)
        return out

    @classmethod
    def resolve(cls, ids: Iterable[str] | None) -> list[type[Check]]:
        canonical = cls.canonical_ids(ids)
        out = [cls._checks[i] for i in canonical]
        return sorted(out, key=lambda c: (c.order, c.id))


def register(cls: type[Check]) -> type[Check]:
    """Decorator used by individual check modules."""
    return Registry.register(cls)
