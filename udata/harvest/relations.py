"""Keep track of cross-references between harvested items to resolve them once all items are known.

Harvested items may reference other items that have yet to be retrieved from the harvest source
(e.g. `dcat:inSeries`, `dcat:servesDataset`). Such references cannot be resolved while an item is
being processed, since the target items may not yet exist on datagouv.

Any `Backend` using a `RelationResolver` must:
1. For each successfully processed item, `register` its `Relation` with the resolver.
2. Once all items have been harvested, `resolve` the relations and persist the updated items.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from udata.core.harvest import Harvestable
from udata.utils import uniquify

log = logging.getLogger(__name__)

SourceT = TypeVar("SourceT", bound=Harvestable)
TargetT = TypeVar("TargetT", bound=Harvestable)


@dataclass(frozen=True, kw_only=True)
class Reference:
    """Identifiers of the target item referenced by a `Relation`."""

    # target's dct:identifier
    identifier: str | None = None
    # target's RDF node URI
    uri: str | None = None

    def __post_init__(self):
        if self.identifier is None and self.uri is None:
            raise ValueError("Reference requires at least one of `identifier` or `uri`")


class ItemIndex:
    """Index of the items used to resolve references, indexed by the different `Harvestable`
    identifiers a reference may point to (see `Relation.match`).

    Items must be indexed once all its identifiers are known and stable. For instance, an
    `item_processor` may change its item's `harvest.remote_id` (see CkanBackend), so indexing must
    happen after.
    """

    def __init__(self, items: Iterable[Harvestable]) -> None:
        self.record_ids: dict[str, Harvestable] = {}
        self.remote_ids: dict[str, Harvestable] = {}
        self.uris: dict[str, Harvestable] = {}
        for item in items:
            # First `item` wins in case of duplicate ids, to match how udata generally handles
            # harvesting duplicates.
            if record_id := item.record_id:
                self.record_ids.setdefault(record_id, item)
            if remote_id := item.harvest.remote_id:
                self.remote_ids.setdefault(remote_id, item)
            if uri := item.harvest.uri:
                self.uris.setdefault(uri, item)


@dataclass(frozen=True)
class Relation(ABC, Generic[SourceT, TargetT]):
    """List of references of a given type, from a source item to its targets.

    A relation is declared between an item of type `SourceT` and one or more items of type
    `TargetT`. Subclasses declare which `target_cls` they accept and define how to `apply` the
    `resolve`d targets to the source item.
    """

    references: Sequence[Reference]

    @property
    @abstractmethod
    def target_cls(self) -> type[TargetT]:
        """Returns the class of items this relation accepts as targets."""

    def resolve(self, index: ItemIndex, source: SourceT) -> list[TargetT]:
        """Resolve references against the items `index`.

        Returns the list of successfully resolved `targets`.
        """
        targets: list[TargetT] = []
        for ref in self.references:
            target = self.match(ref, index)
            if target is None or target is source:
                continue
            targets.append(target)
        return uniquify(targets)

    def match(self, reference: Reference, index: ItemIndex) -> TargetT | None:
        """Match `reference` against the items `index`.

        Relations can override to implement custom match rules.
        """
        if target := self.lookup(reference.identifier, index.remote_ids):
            return target
        if target := self.lookup(reference.uri, index.uris):
            return target

    @abstractmethod
    def apply(self, source: SourceT, targets: Sequence[TargetT]) -> bool:
        """Update `source` item from the resolved `targets`.

        Returns whether item was updated, i.e. needs to be persisted.
        """

    def lookup(self, key: str | None, table: dict[str, Harvestable]) -> TargetT | None:
        """Returns `table[key]` if it is the expected type, else `None`."""
        if key is None:
            return None
        item = table.get(key)
        if item is None:
            return None
        if not isinstance(item, self.target_cls):
            log.warning(
                "Skipping reference %r: resolved target is a %s, expected %s",
                key,
                type(item).__name__,
                self.target_cls.__name__,
            )
            return None
        return item


class RelationResolver:
    """Collect items' relations during a harvest run, then resolve all the references at once, once
    all items have been harvested.
    """

    def __init__(self) -> None:
        # Relations by item, but keyed by `id(item)` and not `item`: MongoEngine `__hash__`/`__eq__`
        # become `pk`-based once `pk` is assigned, and `pk` can be assigned *after* `register` has
        # been called (in dry-run). So an `item`-keyed dict could end up with the same item
        # registered on two keys.
        self._relations: dict[int, tuple[Harvestable, list[Relation[Any, Any]]]] = {}

    def register(self, item: Harvestable, relations: Sequence[Relation[Any, Any]]) -> None:
        """Register the `relations` of an `item`.

        Only successfully persisted `item`s should be registered: `item`s are both sources and
        potential targets of relations, and in both cases skipped/failed items are irrelevant.

        Relations are accumulated per item: a backend may register the same item several times with
        additional relations.
        """
        default_entry: tuple[Harvestable, list[Relation[Any, Any]]] = (item, [])
        entry = self._relations.setdefault(id(item), default_entry)
        entry[1].extend(relations)

    def resolve(self) -> list[Harvestable]:
        """Resolve and apply all previously registered relations.

        Returns the list of items that were updated in the process. The caller is responsible for
        persisting the changes.
        """
        index = ItemIndex(item for item, _ in self._relations.values())
        updated: list[Harvestable] = []
        for source, relations in self._relations.values():
            changed = False
            for relation in relations:
                targets = relation.resolve(index, source=source)
                changed |= relation.apply(source, targets)
            if changed:
                updated.append(source)
        return updated
