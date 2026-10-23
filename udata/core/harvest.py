from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar, cast

from mongoengine import EmbeddedDocument
from mongoengine.fields import DateTimeField, StringField
from rdflib import Node
from rdflib.resource import Resource as RdfResource

from udata.api_fields import field
from udata.core.owned import Owned
from udata.mongo.url_field import URLField

log = logging.getLogger(__name__)


# Prefix of the synthetic record-reference URNs produced by the ISO-19139/SEMIC conversion
# (`urn:iso19139:<type>:<record-uuid>`). Such values resolve against the run-scoped record
# registry, never the resource registry.
ISO19139_URN_PREFIX = "urn:iso19139:"


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


class Harvestable(Owned):
    """Base class for the models that can be harvested."""

    # Type annotation only - Concrete model MUST declare its own `harvest` field.
    harvest: HarvestMetadata

    # FIXME[series]: don't assigne here?
    # Concrete model MUST set its concrete `HarvestMetadata` subclass.
    harvest_metadata_class: type[HarvestMetadata] = HarvestMetadata

    def set_harvested(self) -> None:
        if not self.harvest:
            self.harvest = self.harvest_metadata_class()


H = TypeVar("H", bound=Harvestable)


@dataclass(frozen=True)
class ReferenceValue:
    """A single reference target extracted from RDF, not yet matched to an item.

    `endswith_allowed` mirrors the legacy (pre-refactor) parser behavior: only values that the
    parser treated as a *`dct:identifier`* (literals and inline-identifier values, whatever the
    identifier's RDF type) are candidates for the `endswith` fallback scan. Plain resource URIs
    are matched exactly only.
    """

    value: str
    endswith_allowed: bool


@dataclass(frozen=True)
class PendingRelation(Generic[H]):
    """A pure value describing a cross-reference awaiting resolution.

    Produced by `Relation.from_rdf`, consumed by `RelationResolver`. It carries the strategy
    class (`relation_cls`) and the collected `ReferenceValue`s; the owning item is supplied by
    the resolver at registration time.
    """

    relation_cls: type[Relation[H]]
    identifiers: list[ReferenceValue]


class Relation(ABC, Generic[H]):
    """Base strategy for a DCAT cross-reference (e.g. `dcat:inSeries`, `dcat:servesDataset`).

    Parsing (`identifiers_from_rdf`/`from_rdf`) and resolution (`resolve`) are pure, stateless
    classmethods; only `apply` mutates the owning harvested item. Subclasses declare the
    predicate and the apply behavior; everything else is shared.
    """

    # FIXME: move to DatasetRelation
    # The predicate whose objects are the referenced targets (an rdflib term).
    predicate: ClassVar[Node]

    # The concrete `Harvestable` subclass this relation targets (e.g. `Dataset`), declared by
    # each concrete subclass. Needed at runtime so `resolve_targets` can reject items of the
    # wrong type (a `cast` would only silence the type checker, not catch the mismatch).
    harvestable_cls: ClassVar[type[Harvestable]]

    @classmethod
    @abstractmethod
    def identifiers_from_rdf(cls, node: RdfResource) -> list[ReferenceValue]:
        """Collect the `ReferenceValue`s for `cls.predicate` on an rdflib `Resource`."""
        raise NotImplementedError

    @classmethod
    def from_rdf(cls, node: RdfResource) -> PendingRelation[H] | None:
        """Value builder: wrap `identifiers_from_rdf(node)` in a `PendingRelation`, or `None` if
        no usable reference was collected."""
        refs = cls.identifiers_from_rdf(node)
        if not refs:
            return None
        return PendingRelation(cls, refs)

    @classmethod
    def resolve_targets(
        cls,
        identifiers: Sequence[ReferenceValue],
        resource_registry: Mapping[str, Harvestable],
        remote_ids: Mapping[str, Harvestable],
        record_registry: Mapping[str, Harvestable],
    ) -> Sequence[H]:
        """Resolve the collected `ReferenceValue`s to target items.

        `resource_registry` merges `remote_id` + `harvest.uri` keys for exact matches;
        `remote_ids` holds `remote_id` keys only and is the surface the `endswith` fallback
        scans (mirroring `main`: `item.harvest.remote_id.endswith(id)`); `record_registry` maps
        run-scoped record UUIDs for `urn:iso19139:*` record references.

        The registries are typed `Harvestable` (a run mixes `Dataset`/`Dataservice` items);
        each resolved target is checked against `cls.harvestable_cls` and skipped with a warning
        if it is not of the expected kind, so the returned `Sequence[H]` is both typed and
        runtime-verified.
        """
        targets: list[H] = []
        for ref in identifiers:
            if ref.value.startswith(ISO19139_URN_PREFIX):
                # Record reference (SEMIC/ISO-19139): the value is a synthetic URN encoding a
                # parent *record* UUID, resolved only against the record registry.
                record_uuid = ref.value.rsplit(":", 1)[-1]
                target = record_registry.get(record_uuid)
            else:
                # Resource reference: exact match over `remote_id` + `harvest.uri`, then the
                # `remote_id`-only `endswith` fallback (Europe XSLT sometimes prefixes IDs with
                # the catalog domain).
                target = resource_registry.get(ref.value)
                if target is None and ref.endswith_allowed:
                    # TODO: fix or remove endswith hack
                    target = next((v for k, v in remote_ids.items() if k.endswith(ref.value)), None)
            if target is not None and target not in targets:
                if not isinstance(target, cls.harvestable_cls):
                    log.warning(
                        "Skipping %s reference %r: resolved target is a %s, expected %s",
                        cls.__name__,
                        ref.value,
                        type(target).__name__,
                        cls.harvestable_cls.__name__,
                    )
                    continue
                # The isinstance guard above guarantees `target` is really `cls.harvestable_cls`;
                # the cast only re-binds the wider `Harvestable` static type to `H`.
                targets.append(cast(H, target))
        return targets

    @classmethod
    @abstractmethod
    def apply(cls, owner: Harvestable, targets: Sequence[H]) -> None:
        """Apply the resolved `targets` to `owner`."""
        raise NotImplementedError


class RelationResolver:
    """
    Collect the harvested items and the pending relations they carry as they are processed,
    then resolve every pending relation once the whole run has been walked.

    `*_from_rdf` return declared references as pure `PendingRelation` values; the caller (e.g.
    the DCAT backend's `item_processor`) feeds them to `register()` together with the item.
    Standalone parsing feeds nothing, so nothing is collected. The run owner is responsible for
    calling `resolve_all()` at the end of the run.

    Deliberately not `Generic[H]`: a single run gathers both `Dataset` and `Dataservice` items
    and owners (e.g. `dcat:servesDataset` joins a dataservice to a dataset), so there is no single
    type `H` over which the whole resolver could be soundly parameterized. The per-relation
    type safety lives on `Relation[H]`/`PendingRelation[H]`; here items stay `Harvestable`.

    Pending relations are grouped by their owner. `resolve_all()` iterates the owners, applies every
    resolved relation of an owner, then persists that owner once (unless `dryrun`): minimal delay
    between resolution and save, and a resolution failure only impacts a single owner.

    The registries are class-independent, but they are **not** captured in `register()`: the
    backend only finishes filling an item's `harvest.remote_id` *after* `register()` has returned
    (via `update_harvest_metadata` in the item processor). They are therefore rebuilt from
    `self._items` at resolve time, once the whole run has been walked, mirroring the legacy
    lazily-built registries.
    """

    def __init__(self, dryrun: bool = False):
        self.dryrun = dryrun
        self._items: list[Harvestable] = []
        self._pending: dict[Harvestable, list[PendingRelation]] = defaultdict(list)

    @staticmethod
    def _build_registries(
        items: Sequence[Harvestable],
    ) -> tuple[dict[str, Harvestable], dict[str, Harvestable], dict[str, Harvestable]]:
        """Rebuild the three lookup registries from the run's collected `items`.

        Returns `(resource_registry, remote_ids, record_registry)`:
        - `resource_registry`: exact-match lookup over each item's `harvest.remote_id` +
          `harvest.uri`. Because `remote_id` often falls back to the node URI, the two spaces are
          merged here: a well-formed value matches whichever field it denotes.
        - `remote_ids`: `harvest.remote_id` only — the surface the `endswith` fallback scans
          (main parity). Deliberately excludes URI keys.
        - `record_registry`: run-scoped mapping from a record UUID (`isPrimaryTopicOf`/
          `dct:identifier`) to the item that carries it, exposed through a transient
          `_remote_record_id` attribute that is never persisted. Kept separate from the resource
          registry because record UUIDs can legitimately collide with resource `remote_id` UUIDs.
        """
        resource_registry: dict[str, Harvestable] = {}
        remote_ids: dict[str, Harvestable] = {}
        record_registry: dict[str, Harvestable] = {}
        for item in items:
            harvest = getattr(item, "harvest", None)
            if harvest is not None:
                if harvest.remote_id:
                    resource_registry[harvest.remote_id] = item
                    remote_ids[harvest.remote_id] = item
                if harvest.uri:
                    resource_registry[harvest.uri] = item
            if record_uuid := getattr(item, "_remote_record_id", None):
                record_registry[record_uuid] = item
        return resource_registry, remote_ids, record_registry

    def register(
        self, item: Harvestable, relations: Sequence[PendingRelation] | None = None
    ) -> None:
        self._items.append(item)
        for relation in relations or []:
            self._pending[item].append(relation)

    def resolve_all(self) -> None:
        if not self._pending:
            return
        resource_registry, remote_ids, record_registry = self._build_registries(self._items)
        for owner, relations in self._pending.items():
            mutated = False
            for relation in relations:
                targets = relation.relation_cls.resolve_targets(
                    relation.identifiers,
                    resource_registry,
                    remote_ids,
                    record_registry,
                )
                if targets:
                    relation.relation_cls.apply(owner, targets)
                    mutated = True
            if mutated and not self.dryrun:
                owner.save()  # type: ignore[attr-defined]  # Harvestable is a mixin; concrete models are Documents with save()
