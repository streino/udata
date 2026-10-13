import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from typing import ClassVar

from mongoengine import EmbeddedDocument
from mongoengine.fields import DateTimeField, StringField
from rdflib import Node

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


# FIXME[series]: improve comments
@dataclass(frozen=True)
class PendingRelation(ABC):
    """A cross-reference between harvested items awaiting resolution; the subclass is its own kind.

    `owner` is the harvested item this relation was collected on; it is provided by
    `Harvestable.add_pending_relation()`, which constructs the relation already attached to its
    owner.
    """

    # `dct:identifier` values collected from the predicate objects, matched against the
    # target's harvested `remote_id`.
    identifiers: list[str]

    # `rdf:resource` object URIs collected from the predicate objects, matched against the
    # target's harvested `harvest.uri`.
    uris: list[str]

    owner: "Harvestable"

    predicate: ClassVar[Node]

    @classmethod
    @abstractmethod
    def from_rdf(cls, node):
        """Collect the `(identifiers, uris)` for the relation from the objects of `cls.predicate`
        on an rdflib `Resource`."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def lookup_keys(cls, item):
        """Yield the keys under which `item` should be findable in the registry."""
        raise NotImplementedError

    @abstractmethod
    def resolve(self, registry):
        """Return the target items for this collected relation."""
        raise NotImplementedError

    @abstractmethod
    def apply(self, resolver, targets):
        """Apply the resolved `targets` to `self.owner`."""
        raise NotImplementedError

    # FIXME[series]: standard python object repr
    def __repr__(self):
        return (
            f"PendingRelation(owner={type(self.owner).__name__}({getattr(self.owner, 'id', None)}), "
            f"identifiers={self.identifiers}, uris={self.uris})"
        )


# FIXME[series]: add tests?
class Harvestable(Owned):
    """Base class for the models that can be harvested."""

    # Type annotation only: each concrete model declares its own `harvest` field.
    harvest: HarvestMetadata

    # FIXME[series]: add comment
    harvest_metadata_class: type[HarvestMetadata] = HarvestMetadata

    def set_harvested(self):
        if not self.harvest:
            self.harvest = self.harvest_metadata_class()

    # FIXME[series]: avoid __setattr__/__getattribute__?
    def add_pending_relation(self, relation_cls, node) -> None:
        identifiers, uris = relation_cls.from_rdf(node)
        if not identifiers and not uris:
            return
        relation = relation_cls(identifiers=identifiers, uris=uris, owner=self)
        log.info(f"REFS: add_pending_relation: relation={type(relation).__name__}, {relation=}")
        object.__setattr__(self, "_pending_references", self.pending_relations() + [relation])

    def pending_relations(self):
        try:
            return object.__getattribute__(self, "_pending_references")
        except AttributeError:
            return []


class RelationResolver:
    """
    Collect the harvested items (and the pending relations they carry) as they are processed,
    then resolve every pending relation once the whole run has been walked.
    """

    def __init__(self, dryrun: bool = False):
        self.dryrun = dryrun
        self._items: list = []
        self._pending: dict[type[PendingRelation], list[PendingRelation]] = defaultdict(list)
        # Run-scoped mapping from a record UUID (`isPrimaryTopicOf`/`dct:identifier`) to the
        # item that carries it. Items expose it through a transient `_remote_record_id`
        # attribute that is never persisted, so this mapping only lives for one harvest run.
        self._record_uuids: dict[str, object] = {}

    def register(self, item) -> None:
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

    def _build_registry(self, ref_cls) -> dict:
        registry = {}
        for item in self._items:
            for key in ref_cls.lookup_keys(item):
                registry[key] = item
        return registry
