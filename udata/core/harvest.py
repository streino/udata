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


# FIXME[series]: more specific name
# FIXME[series]: improve comments
@dataclass
class Reference(ABC):
    """A collected cross-reference; the subclass is its own kind."""

    identifiers: list[str]
    uris: list[str]

    predicate: ClassVar[Node]

    @classmethod
    @abstractmethod
    def from_rdf(cls, node):
        """Collect the reference data from the objects of `cls.predicate` on an rdflib `Resource`."""
        raise NotImplementedError

    # FIXME[series]: rename index -> lookup?
    @classmethod
    @abstractmethod
    def index_keys(cls, item):
        """Yield the keys under which `item` should be findable in the registry."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def resolve(cls, registry, reference):
        """Return the target items for a collected reference of this kind."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def apply(cls, resolver, owner, targets):
        """Apply the resolved `targets` to the `owner` item."""
        raise NotImplementedError


# FIXME[series]: improve comments
@dataclass
class PendingReference:
    """A reference collected on `owner` that is waiting to be resolved."""

    owner: "Harvestable"
    reference: Reference

    # FIXME[series]: standard python object repr
    def __repr__(self):
        return (
            f"Pending(owner={type(self.owner).__name__}({getattr(self.owner, 'id', None)}), "
            f"reference={type(self.reference).__name__}({self.reference}))"
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
    # FIXME[series]: coherent func names (pending vs pending_references)
    def add_pending(self, reference: Reference) -> None:
        log.info(f"REFS: add_pending: reference={type(reference).__name__}, {reference=}")
        pending = PendingReference(owner=self, reference=reference)
        object.__setattr__(self, "_pending_references", self.pending_references() + [pending])

    def pending_references(self):
        try:
            return object.__getattribute__(self, "_pending_references")
        except AttributeError:
            return []


class ReferenceResolver:
    """
    Collect the harvested items (and the pending references they carry) as they are processed,
    then resolve every pending reference once the whole run has been walked.
    """

    def __init__(self, dryrun: bool = False):
        self.dryrun = dryrun
        self._items: list = []
        self._pending: dict[type[Reference], list[PendingReference]] = defaultdict(list)
        # Run-scoped mapping from a record UUID (`isPrimaryTopicOf`/`dct:identifier`) to the
        # item that carries it. Items expose it through a transient `_remote_record_id`
        # attribute that is never persisted, so this mapping only lives for one harvest run.
        self._record_uuids: dict[str, object] = {}

    def register(self, item) -> None:
        log.info(
            f"REFS: register: item={type(item).__name__}({getattr(item, 'id', None)}), "
            f"pending_kinds={[type(p.reference).__name__ for p in item.pending_references()]}"
        )
        self._items.append(item)
        if record_uuid := getattr(item, "_remote_record_id", None):
            self._record_uuids[record_uuid] = item
        for pending in item.pending_references():
            self._pending[type(pending.reference)].append(pending)

    def resolve(self) -> None:
        log.info("REFS: resolving...")
        for ref_cls, pending_references in self._pending.items():
            registry = self._build_registry(ref_cls)
            registry.update(self._record_uuids)
            log.info(f"REFS: resolve: kind={ref_cls.__name__}, {registry=}")
            for pending in pending_references:
                targets = ref_cls.resolve(registry, pending.reference)
                if targets:
                    log.info(
                        f"REFS: resolve: kind={ref_cls.__name__}, "
                        f"owner={type(pending.owner).__name__}({getattr(pending.owner, 'id', None)}), "
                        f"{targets=}"
                    )
                    ref_cls.apply(self, pending.owner, targets)

    def _build_registry(self, ref_cls) -> dict:
        registry = {}
        for item in self._items:
            for key in ref_cls.index_keys(item):
                registry[key] = item
        return registry
