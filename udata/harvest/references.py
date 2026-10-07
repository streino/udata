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

from collections import defaultdict

from udata.references import PendingReference, ReferenceKind


class DatasetReferenceKind(ReferenceKind):
    """Common logic for references whose target is a harvested `Dataset`."""

    def index_keys(self, item):
        from udata.core.dataset.models import Dataset

        if not isinstance(item, Dataset) or item.harvest is None:
            return
        if item.harvest.remote_id:
            yield item.harvest.remote_id
        if item.harvest.uri:
            yield item.harvest.uri

    def resolve(self, registry, data):
        targets = []
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
        return targets


class ServesDatasetsKind(DatasetReferenceKind):
    """A dataservice serves one or more datasets (`dcat:servesDataset`)."""

    name = "serves_dataset"

    def apply(self, resolver, owner, targets):
        for dataset in targets:
            if dataset not in owner.datasets:
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

    def register(self, item) -> None:
        self._items.append(item)
        for pending in item.pending_references():
            self._pending[pending.kind].append(pending)

    def resolve(self) -> None:
        for kind, pending_references in self._pending.items():
            registry = self._build_registry(kind)
            for pending in pending_references:
                targets = kind.resolve(registry, pending.data)
                if targets:
                    kind.apply(self, pending.owner, targets)

    def _build_registry(self, kind) -> dict:
        registry = {}
        for item in self._items:
            for key in kind.index_keys(item):
                registry[key] = item
        return registry
