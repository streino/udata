"""Keep track of cross-references between harvested items to resolve them once all items are known.

Harvested items may reference other items that have yet to be retrieved from the harvest source
(e.g. `dcat:inSeries`, `dcat:servesDataset`). Such references cannot be resolved while an item is
being processed, since the target items may not yet exist on datagouv.

Any `Backend` using a `RelationResolver` must:
1. For each successfully processed item, `register` its `Relation` with the resolver.
2. Once all items have been harvested, `resolve` the relations and persist the updated items.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from udata.core.harvest import Harvestable
from udata.utils import uniquify

if TYPE_CHECKING:
    from udata.harvest.models import HarvestItem

log = logging.getLogger(__name__)

Source = TypeVar("Source", bound=Harvestable)
Target = TypeVar("Target", bound=Harvestable)


@dataclass(frozen=True, kw_only=True)
class ItemReference:
    """Identifiers of an item, which can be referenced in a `Relation`."""

    # item's dct:identifier
    identifier: str | None = None
    # item's RDF node URI
    uri: str | None = None

    def __post_init__(self):
        if self.identifier is None and self.uri is None:
            raise ValueError("Reference requires at least one of `identifier` or `uri`")


class ItemRegistry:
    """Registry of harvested items, used to resolve relation references.

    Indexed by the different `Harvestable` identifiers a reference may point to. See
    `Relation.match()`.

    Items must be registered once all its identifiers are known and stable. For instance, some
    implementations of `parse_item()` can change an item's `harvest.remote_id`, so registering must
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
class Relation(ABC, Generic[Source, Target]):
    """List of an item's relations of a given type.

    A relation is declared between a `Source` and one or more `Target`. Subclasses declare which
    `target_cls` they accept and define how to `apply()` the `resolve()`d targets to the source
    item.
    """

    references: Sequence[ItemReference]

    @property
    @abstractmethod
    def target_cls(self) -> type[Target]:
        """Returns the class of items this relation accepts as targets."""

    def resolve(self, source: Source, registry: ItemRegistry) -> list[Target]:
        """Resolve references against the items in `registry`.

        Returns the list of successfully resolved `targets`.
        """
        targets: list[Target] = []
        for ref in self.references:
            target = self.match(ref, registry)
            if target is None or target is source:
                continue
            targets.append(target)
        return uniquify(targets)

    def match(self, reference: ItemReference, registry: ItemRegistry) -> Target | None:
        """Match `reference` against the items in `registry`.

        Relations can override to implement custom match rules.
        """
        if target := self.lookup(reference.identifier, registry.remote_ids):
            return target
        if target := self.lookup(reference.uri, registry.uris):
            return target

    @abstractmethod
    def apply(self, source: Source, targets: Sequence[Target]) -> bool:
        """Update `source` item from the resolved `targets`.

        Returns whether item was updated, i.e. needs to be persisted.
        """

    def lookup(self, key: str | None, index: dict[str, Harvestable]) -> Target | None:
        """Returns `index[key]` if it is the expected type, else `None`."""
        if key is None:
            return None
        item = index.get(key)
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
    """Collect items' relations during a harvest run, then resolve all references once all items
    are known.

    Usage:
    1. For each successfully processed item, call `register(harvest_item, relations)`.
    2. Once the harvest loop is done, iterate `resolve(job.items)` and persist the updated items.
    """

    resolvable_statuses: ClassVar[frozenset[str]] = frozenset({"started", "done"})

    @staticmethod
    def register(harvest_item: HarvestItem, relations: Sequence[Relation[Any, Any]]) -> None:
        """Record the relations of a successfully processed item for later resolution."""
        harvest_item.relations += relations

    @staticmethod
    def resolve(harvest_items: Iterable[HarvestItem]) -> Iterator[HarvestItem]:
        """Resolve every item's relations against all resolvable items in a job, in a single pass.

        Yields the updated `HarvestItem`s, so the caller can persist their referenced
        `Harvestable`s.
        """
        resolvable = [
            hi for hi in harvest_items if hi.status in RelationResolver.resolvable_statuses
        ]
        registry = ItemRegistry(hi.item for hi in resolvable)
        # FIXME hi.item can be None
        for hi in resolvable:
            if not hi.relations:
                continue
            changed = False
            for relation in hi.relations:
                targets = relation.resolve(hi.item, registry)
                changed |= relation.apply(hi.item, targets)
            if changed:
                yield hi
