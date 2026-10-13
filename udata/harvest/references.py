"""Cross-reference resolution between harvested items.

The DcatBackend walks several RDF pages and a given record may reference another one
(a dataservice `dcat:servesDataset`, a dataset `dcat:inSeries`, ...) that is defined on a
different page of the same harvest. Those cross-references cannot be resolved while parsing a
single record, so they are collected as pending references on the items themselves and resolved
in a second pass over everything that was seen during the run.

This module lives in the harvest layer because resolving references requires knowing the concrete
models (a `dcat:servesDataset` target is a `Dataset`), while the generic pending-reference
machinery (`Reference`, `ReferenceKind`, `PendingReference`, `references_from_rdf`) is kept as a
dependency-free leaf in `udata.references`.
"""

import logging
from collections import defaultdict

from udata.references import PendingReference, ReferenceKind

log = logging.getLogger(__name__)

# A `dcat:inSeries` (and other cross-references) can take two distinct forms, both handled by
# `DatasetReferenceKind.resolve`:
#
# 1. A *resource reference* (standard, e.g. GeoNetwork): the predicate object is the parent
#    resource itself, referenced by its `dct:identifier` or resource URI. Resolved against the
#    harvested parent's `remote_id` / `harvest.uri`.
# 2. A *record reference* (SEMIC/ISO-19139): the predicate object is a synthetic URN encoding a
#    parent *record* UUID (`urn:iso19139:<type>:<record-uuid>`), not a resource URI. Resolved
#    through the run-scoped record UUID -> item mapping in `ReferenceResolver._record_uuids`.
ISO19139_URN_PREFIX = "urn:iso19139:"


class DatasetReferenceKind(ReferenceKind):
    """Common logic for references whose target is a harvested `Dataset`."""

    def index_keys(self, item):
        from udata.core.dataset.models import Dataset

        log.info(f"REFS: index_keys: {item=}, {item.harvest=}")
        if not isinstance(item, Dataset) or item.harvest is None:
            return
        if item.harvest.remote_id:
            yield item.harvest.remote_id
        if item.harvest.uri:
            yield item.harvest.uri

    def resolve(self, registry, data):
        targets = []

        # Form 1 — resource reference (standard, e.g. GeoNetwork): the predicate object is the
        # parent resource itself, referenced by its `dct:identifier` or by its resource URI. Both
        # are indexed from the harvested parent's `remote_id` and `harvest.uri` (see `index_keys`).
        for identifier in data.identifiers:
            target = registry.get(identifier)
            if target is None:
                # Europe XSLT sometimes prefixes identifiers with the catalog domain.
                target = next(
                    (
                        v
                        for k, v in registry.items()
                        if isinstance(k, str) and k.endswith(identifier)
                    ),
                    None,
                )
            if target is not None and target not in targets:
                targets.append(target)
        for uri in data.uris:
            target = registry.get(uri)
            if target is not None and target not in targets:
                targets.append(target)

        # Form 2 — record reference (SEMIC/ISO-19139): the URI is a synthetic URN encoding the
        # parent *record* UUID (`urn:iso19139:<type>:<record-uuid>`), not a resource URI. There is
        # no matching `remote_id`/`harvest.uri`, so resolve through the run-scoped record UUID
        # mapping (`ReferenceResolver._record_uuids`) instead.
        for uri in data.uris:
            if not uri.startswith(ISO19139_URN_PREFIX):
                continue
            record_uuid = uri.rsplit(":", 1)[-1]
            target = registry.get(record_uuid)
            if target is not None and target not in targets:
                targets.append(target)

        log.info(f"REFS: resolve: {targets=}")
        return targets


class ServesDatasetsKind(DatasetReferenceKind):
    """A dataservice serves one or more datasets (`dcat:servesDataset`)."""

    name = "serves_dataset"

    def apply(self, resolver, owner, targets):
        for dataset in targets:
            if dataset not in owner.datasets:
                log.info(f"REFS: apply: datasets += Dataset({dataset.id})")
                owner.datasets.append(dataset)
        if not resolver.dryrun:
            owner.save()


class InSeriesKind(DatasetReferenceKind):
    """A dataset belongs to a series (`dcat:inSeries`)."""

    name = "in_series"

    def apply(self, resolver, owner, targets):
        if not targets:
            return
        parent = targets[0]
        from udata.core.dataset.rdf import set_dcat_extra

        log.info(f"REFS: apply: extras.dcat.in_series = {str(parent.id)}")
        set_dcat_extra(owner, "in_series", str(parent.id))
        if not resolver.dryrun:
            owner.save()


SERVES_DATASETS = ServesDatasetsKind()
IN_SERIES = InSeriesKind()


class ReferenceResolver:
    """
    Collect the harvested items (and the pending references they carry) as they are processed,
    then resolve every pending reference once the whole run has been walked.
    """

    def __init__(self, dryrun: bool = False):
        self.dryrun = dryrun
        self._items: list = []
        self._pending: dict[ReferenceKind, list[PendingReference]] = defaultdict(list)
        # Run-scoped mapping from a record UUID (`isPrimaryTopicOf`/`dct:identifier`) to the
        # item that carries it. Items expose it through a transient `_remote_record_id`
        # attribute that is never persisted, so this mapping only lives for one harvest run.
        self._record_uuids: dict[str, object] = {}

    def register(self, item) -> None:
        log.info(
            f"REFS: register: item={type(item).__name__}({getattr(item, 'id', None)}), pending_kinds={[p.kind.name for p in item.pending_references()]}"
        )
        self._items.append(item)
        if record_uuid := getattr(item, "_remote_record_id", None):
            self._record_uuids[record_uuid] = item
        for pending in item.pending_references():
            self._pending[pending.kind].append(pending)

    def resolve(self) -> None:
        log.info("REFS: resolving...")
        for kind, pending_references in self._pending.items():
            registry = self._build_registry(kind)
            registry.update(self._record_uuids)
            log.info(f"REFS: resolve: kind={kind.name}, {registry=}")
            for pending in pending_references:
                targets = kind.resolve(registry, pending.data)
                if targets:
                    log.info(
                        f"REFS: resolve: kind={kind.name}, owner={type(pending.owner).__name__}({getattr(pending.owner, 'id', None)}), {targets=}"
                    )
                    kind.apply(self, pending.owner, targets)

    def _build_registry(self, kind) -> dict:
        registry = {}
        for item in self._items:
            for key in kind.index_keys(item):
                registry[key] = item
        return registry
