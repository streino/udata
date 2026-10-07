"""Unit tests for the reference resolution machinery introduced with the harvest refactor.

These tests exercise `udata.references` (the dependency-free leaf) and
`udata.harvest.references` (the harvest-aware resolver and concrete kinds). Real `Dataset`
instances are used where only pure attribute/dict access is involved (`index_keys`, `resolve`).
The `apply` step mutates the `datasets` ReferenceField (which needs saved documents), so the
resolver orchestration and the `apply` methods are tested against small fakes. No database is
required.
"""

import logging

from rdflib import BNode, Graph, Literal, Namespace, URIRef

from udata.core.dataservices.models import Dataservice
from udata.core.dataset.models import Dataset
from udata.harvest.references import (
    IN_SERIES,
    SERVES_DATASETS,
    DatasetReferenceKind,
    ReferenceResolver,
)
from udata.references import PendingReference, Reference, ReferenceKind, references_from_rdf

DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")


def build_dataset(remote_id=None, uri=None) -> Dataset:
    harvest = {}
    if remote_id:
        harvest["remote_id"] = remote_id
    if uri:
        harvest["uri"] = uri
    return Dataset(harvest=harvest)


class ReferencesFromRdfTest:
    def test_by_uri(self):
        graph = Graph()
        node = URIRef("http://ex/dataservice")
        target = URIRef("http://ex/dataset/1")
        graph.add((node, DCAT.servesDataset, target))

        reference = references_from_rdf(graph.resource(node), DCAT.servesDataset)

        assert reference.identifiers == []
        assert reference.uris == ["http://ex/dataset/1"]

    def test_mixed_and_empty(self):
        graph = Graph()
        node = URIRef("http://ex/dataservice")
        by_id = URIRef("http://ex/a")
        by_uri = URIRef("http://ex/b")
        graph.add((node, DCAT.servesDataset, by_id))
        graph.add((node, DCAT.servesDataset, by_uri))
        graph.add((by_id, DCT.identifier, Literal("A")))

        reference = references_from_rdf(graph.resource(node), DCAT.servesDataset)

        # Inline identifier should be ignored; only the URI should be collected.
        assert reference.identifiers == []
        assert reference.uris == ["http://ex/b"]

        empty = references_from_rdf(graph.resource(URIRef("http://ex/other")), DCAT.servesDataset)
        assert empty.identifiers == []
        assert empty.uris == []

    def test_inline_ignored_with_warning(self, caplog):
        """Inline `dcat:Dataset` (no rdf:resource) should be ignored and a warning logged."""
        graph = Graph()
        node = URIRef("http://ex/service")
        inline = BNode()
        graph.add((node, DCAT.servesDataset, inline))
        graph.add((inline, DCT.identifier, Literal("ignored")))
        with caplog.at_level(logging.WARNING):
            ref = references_from_rdf(graph.resource(node), DCAT.servesDataset)
        assert ref.identifiers == []
        assert ref.uris == []
        assert any("Ignoring inline" in rec.message for rec in caplog.records)

    def test_blank_node_ignored_with_warning(self, caplog):
        """A blank node without rdf:resource should also be ignored with a warning."""
        graph = Graph()
        node = URIRef("http://ex/service")
        blank = BNode()
        graph.add((node, DCAT.servesDataset, blank))  # no rdf:resource, no identifier
        with caplog.at_level(logging.WARNING):
            ref = references_from_rdf(graph.resource(node), DCAT.servesDataset)
        assert ref.identifiers == []
        assert ref.uris == []
        assert any("Ignoring inline" in rec.message for rec in caplog.records)


class DatasetReferenceKindTest:
    def test_index_keys_uses_remote_id_and_uri(self):
        kind = DatasetReferenceKind()
        dataset = build_dataset(remote_id="1", uri="http://ex/1")

        assert set(kind.index_keys(dataset)) == {"1", "http://ex/1"}

    def test_index_keys_ignores_non_datasets(self):
        kind = DatasetReferenceKind()
        service = Dataservice(harvest={"remote_id": "s"})

        assert list(kind.index_keys(service)) == []

    def test_resolve_by_identifier_exact(self):
        d1 = build_dataset(remote_id="1")
        d2 = build_dataset(remote_id="2")
        registry = {"1": d1, "2": d2}

        targets = DatasetReferenceKind().resolve(registry, Reference(identifiers=["2"], uris=[]))

        assert targets == [d2]

    def test_resolve_endswith_fallback(self):
        d1 = build_dataset(remote_id="http://data.test.org/1")
        registry = {"http://data.test.org/1": d1}

        targets = DatasetReferenceKind().resolve(registry, Reference(identifiers=["1"], uris=[]))

        assert targets == [d1]

    def test_resolve_by_uri(self):
        d1 = build_dataset(uri="http://ex/1")
        registry = {"http://ex/1": d1}

        targets = DatasetReferenceKind().resolve(
            registry, Reference(identifiers=[], uris=["http://ex/1"])
        )

        assert targets == [d1]

    def test_resolve_dedups(self):
        d1 = build_dataset(remote_id="1", uri="http://ex/1")
        registry = {"1": d1, "http://ex/1": d1}

        targets = DatasetReferenceKind().resolve(
            registry, Reference(identifiers=["1"], uris=["http://ex/1"])
        )

        assert targets == [d1]

    def test_resolve_unknown_returns_empty(self):
        registry = {"1": build_dataset(remote_id="1")}

        targets = DatasetReferenceKind().resolve(registry, Reference(identifiers=["nope"], uris=[]))

        assert targets == []


class _FakeOwner:
    def __init__(self, datasets=None):
        self.datasets = datasets or []
        self.saved = False

    def save(self):
        self.saved = True


class ServesDatasetsKindTest:
    def test_apply_appends_targets(self):
        d1 = build_dataset(remote_id="1")
        d2 = build_dataset(remote_id="2")
        owner = _FakeOwner()

        SERVES_DATASETS.apply(type("R", (), {"dryrun": True})(), owner, [d1, d2])

        assert owner.datasets == [d1, d2]

    def test_apply_dedups_existing(self):
        d1 = build_dataset(remote_id="1")
        owner = _FakeOwner(datasets=[d1])

        SERVES_DATASETS.apply(type("R", (), {"dryrun": True})(), owner, [d1, d1])

        assert owner.datasets == [d1]

    def test_apply_live_run_saves(self):
        owner = _FakeOwner()

        SERVES_DATASETS.apply(
            type("R", (), {"dryrun": False})(), owner, [build_dataset(remote_id="1")]
        )

        assert owner.saved is True

    def test_apply_dryrun_does_not_save(self):
        owner = _FakeOwner()

        SERVES_DATASETS.apply(
            type("R", (), {"dryrun": True})(), owner, [build_dataset(remote_id="1")]
        )

        assert owner.saved is False


class InSeriesKindTest:
    def test_apply_sets_in_series_extra(self):
        child = build_dataset(remote_id="child")
        parent = build_dataset(remote_id="parent")
        parent.id = "parent_id"

        IN_SERIES.apply(type("R", (), {"dryrun": True})(), child, [parent])

        assert child.extras["dcat"]["in_series"] == "parent_id"

    def test_apply_empty_targets_is_noop(self):
        child = build_dataset(remote_id="child")

        IN_SERIES.apply(type("R", (), {"dryrun": True})(), child, [])

        assert "dcat" not in child.extras


class _FakeKind(ReferenceKind):
    name = "fake"

    def index_keys(self, item):
        yield item.key

    def resolve(self, registry, data):
        return [registry[k] for k in data if k in registry]

    def apply(self, resolver, owner, targets):
        owner.resolved = [t.key for t in targets]


class _FakeItem:
    def __init__(self, key):
        self.key = key
        self._pending = []

    def add_pending(self, kind, data):
        self._pending.append(PendingReference(owner=self, kind=kind, data=data))

    def pending_references(self):
        return self._pending


class ReferenceResolverTest:
    def test_register_collects_pending_and_resolves(self):
        kind = _FakeKind()
        source = _FakeItem("service")
        source.add_pending(kind, ["d1", "d2"])
        target1 = _FakeItem("d1")
        target2 = _FakeItem("d2")

        resolver = ReferenceResolver(dryrun=True)
        for item in (target1, target2, source):
            resolver.register(item)
        resolver.resolve()

        assert source.resolved == ["d1", "d2"]

    def test_resolve_without_pending_is_noop(self):
        target = _FakeItem("d1")
        resolver = ReferenceResolver(dryrun=True)
        resolver.register(target)
        resolver.resolve()

        assert not hasattr(target, "resolved")

    def test_resolve_unknown_target_is_skipped(self):
        kind = _FakeKind()
        source = _FakeItem("service")
        source.add_pending(kind, ["d1", "nope"])
        target1 = _FakeItem("d1")

        resolver = ReferenceResolver(dryrun=True)
        resolver.register(target1)
        resolver.register(source)
        resolver.resolve()

        assert source.resolved == ["d1"]
