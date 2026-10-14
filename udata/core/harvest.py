from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from mongoengine import EmbeddedDocument
from mongoengine.fields import DateTimeField, StringField
from rdflib import Node
from rdflib.resource import Resource as RdfResource

from udata.api_fields import field
from udata.core.owned import Owned
from udata.mongo.url_field import URLField

log = logging.getLogger(__name__)


class HarvestMetadata(EmbeddedDocument):
    meta = {"abstract": True}

    backend = field(StringField())

    domain = field(StringField())

    source_id = field(StringField())
    source_url = field(URLField())

    # TODO: move `uri` field here (need to converge on type)

    remote_id = field(StringField())
    remote_url = field(URLField())

    created_at = field(
        DateTimeField(), description="Date of creation as provided by the harvested catalog"
    )
    issued_at = field(
        DateTimeField(), description="Date of release as provided by the harvested catalog"
    )
    modified_at = field(
        DateTimeField(),
        description="Date of last modification as provided by the harvested catalog",
    )

    last_update = field(DateTimeField(), description="Date of last harvest")

    archived_at = field(DateTimeField())
    archived_reason = field(StringField())


# FIXME[series]: add tests?
class Harvestable(Owned):
    """Base class for the models that can be harvested."""

    # Type annotation only: each concrete model declares its own `harvest` field.
    harvest: HarvestMetadata

    # FIXME[series]: add comment
    harvest_metadata_class: type[HarvestMetadata] = HarvestMetadata

    def set_harvested(self) -> None:
        if not self.harvest:
            self.harvest = self.harvest_metadata_class()

    # FIXME[series]: avoid __setattr__/__getattribute__?
    def add_pending_relation(self, relation_cls: type[PendingRelation], node: RdfResource) -> None:
        identifiers, uris = relation_cls.from_rdf(node)
        if not identifiers and not uris:
            return
        relation = relation_cls(identifiers=identifiers, uris=uris, owner=self)
        log.info(f"REFS: add_pending_relation: relation={type(relation).__name__}, {relation=}")
        object.__setattr__(self, "_pending_references", self.pending_relations() + [relation])

    def pending_relations(self) -> list[PendingRelation]:
        try:
            return object.__getattribute__(self, "_pending_references")
        except AttributeError:
            return []


H = TypeVar("H", bound="Harvestable")


# FIXME[series]: improve comments
@dataclass(frozen=True)
class PendingRelation(ABC, Generic[H]):
    """A cross-reference between harvested items awaiting resolution; the subclass is its own kind.

    `owner` is the harvested item this relation was collected on; it is provided by
    `Harvestable.add_pending_relation()`, which constructs the relation already attached to its
    owner.

    The class is generic over the type of the *target* items that `resolve()` returns (e.g.
    `Dataset` for `dcat:inSeries`), so subclasses can specialize `resolve`/`apply` precisely.
    """

    # `dct:identifier` values collected from the predicate objects, matched against the
    # target's harvested `remote_id`.
    identifiers: list[str]

    # `rdf:resource` object URIs collected from the predicate objects, matched against the
    # target's harvested `harvest.uri`.
    uris: list[str]

    owner: Harvestable

    predicate: ClassVar[Node]

    @classmethod
    @abstractmethod
    def from_rdf(cls, node: RdfResource) -> tuple[list[str], list[str]]:
        """Collect the `(identifiers, uris)` for the relation from the objects of `cls.predicate`
        on an rdflib `Resource`."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def lookup_keys(cls, item: Harvestable) -> Iterator[str]:
        """Yield the keys under which `item` should be findable in the registry."""
        raise NotImplementedError

    @abstractmethod
    def resolve(self, registry: Mapping[str, Harvestable]) -> Sequence[H]:
        """Return the target items for this collected relation."""
        raise NotImplementedError

    @abstractmethod
    def apply(self, resolver: RelationResolver, targets: Sequence[H]) -> None:
        """Apply the resolved `targets` to `self.owner`."""
        raise NotImplementedError

    # FIXME[series]: standard python object repr
    def __repr__(self):
        return (
            f"PendingRelation(owner={type(self.owner).__name__}({getattr(self.owner, 'id', None)}), "
            f"identifiers={self.identifiers}, uris={self.uris})"
        )


class RelationResolver:
    """
    Collect the harvested items (and the pending relations they carry) as they are processed,
    then resolve every pending relation once the whole run has been walked.
    """

    def __init__(self, dryrun: bool = False):
        self.dryrun = dryrun
        self._items: list[Harvestable] = []
        self._pending: dict[type[PendingRelation], list[PendingRelation]] = defaultdict(list)
        # Run-scoped mapping from a record UUID (`isPrimaryTopicOf`/`dct:identifier`) to the
        # item that carries it. Items expose it through a transient `_remote_record_id`
        # attribute that is never persisted, so this mapping only lives for one harvest run.
        self._record_uuids: dict[str, Harvestable] = {}

    def register(self, item: Harvestable) -> None:
        log.info(
            f"REFS: register: item={type(item).__name__}({getattr(item, 'id', None)}), "
            f"pending_kinds={[type(p).__name__ for p in item.pending_relations()]}"
        )
        self._items.append(item)
        if record_uuid := getattr(item, "_remote_record_id", None):
            self._record_uuids[record_uuid] = item
        for relation in item.pending_relations():
            self._pending[type(relation)].append(relation)

    def resolve(self) -> None:
        log.info("REFS: resolving...")
        for ref_cls, relations in self._pending.items():
            registry = self._build_registry(ref_cls)
            registry.update(self._record_uuids)
            log.info(f"REFS: resolve: kind={ref_cls.__name__}, {registry=}")
            for relation in relations:
                targets = relation.resolve(registry)
                if targets:
                    log.info(
                        f"REFS: resolve: kind={ref_cls.__name__}, "
                        f"owner={type(relation.owner).__name__}({getattr(relation.owner, 'id', None)}), "
                        f"{targets=}"
                    )
                    relation.apply(self, targets)

    def _build_registry(self, ref_cls: type[PendingRelation]) -> dict[str, Harvestable]:
        registry = {}
        for item in self._items:
            for key in ref_cls.lookup_keys(item):
                registry[key] = item
        return registry
