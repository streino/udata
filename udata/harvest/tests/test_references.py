"""Unit tests for the reference resolution machinery introduced with the harvest refactor.

These tests exercise `udata.core.harvest` (the dependency-free `Reference` base,
`PendingReference`, and the `ReferenceResolver`) and the concrete reference subclasses in
`udata.core.dataset.rdf` / `udata.core.dataservices.rdf`. Real `Dataset` instances are used where
only pure attribute/dict access is involved (`index_keys`, `resolve`). The `apply` step mutates
the `datasets` ReferenceField (which needs saved documents), so the resolver orchestration and the
`apply` methods are tested against small fakes. No database is required.
"""

import logging

from rdflib import BNode, Graph, Literal, Namespace, URIRef

from udata.core.dataservices.models import Dataservice
from udata.core.dataservices.rdf import ServesDatasetsReference
from udata.core.dataset.models import Dataset
from udata.core.dataset.rdf import DatasetReference, InSeriesReference
from udata.core.harvest import PendingReference, Reference, ReferenceResolver

DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")


def build_dataset(remote_id=None, uri=None, record_uuid=None) -> Dataset:
    harvest = {}
    if remote_id:
        harvest["remote_id"] = remote_id
    if uri:
        harvest["uri"] = uri
    dataset = Dataset(harvest=harvest)
    if record_uuid:
        dataset._remote_record_id = record_uuid
    return dataset


class ReferencesFromRdfTest:
    def test_by_uri(self):
        graph = Graph()
        node = URIRef("http://ex/dataservice")
        target = URIRef("http://ex/dataset/1")
        graph.add((node, DCAT.servesDataset, target))

        reference = ServesDatasetsReference.from_rdf(graph.resource(node))

        assert reference.identifiers == []
        assert reference.uris == ["http://ex/dataset/1"]

    def test_resource_reference_without_identifier_is_a_uri(self):
        """A `dcat:inSeries` object that is a plain resource URI (no dct:identifier on the node)
        must be recorded as a URI, not an identifier."""
        graph = Graph()
        node = URIRef("http://ex/child")
        parent = URIRef("http://ex/api/records/parent")
        graph.add((node, DCAT.inSeries, parent))

        reference = InSeriesReference.from_rdf(graph.resource(node))

        assert reference.identifiers == []
        assert reference.uris == ["http://ex/api/records/parent"]

    def test_mixed_and_empty(self):
        graph = Graph()
        node = URIRef("http://ex/dataservice")
        by_id = URIRef("http://ex/a")
        by_uri = URIRef("http://ex/b")
        graph.add((node, DCAT.servesDataset, by_id))
        graph.add((node, DCAT.servesDataset, by_uri))
        graph.add((by_id, DCT.identifier, Literal("A")))

        reference = ServesDatasetsReference.from_rdf(graph.resource(node))

        # Inline identifier should be ignored; only the URI should be collected.
        assert reference.identifiers == []
        assert reference.uris == ["http://ex/b"]

        empty = ServesDatasetsReference.from_rdf(graph.resource(URIRef("http://ex/other")))
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
            ref = ServesDatasetsReference.from_rdf(graph.resource(node))
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
            ref = ServesDatasetsReference.from_rdf(graph.resource(node))
        assert ref.identifiers == []
        assert ref.uris == []
        assert any("Ignoring inline" in rec.message for rec in caplog.records)


class DatasetReferenceTest:
    def test_index_keys_uses_remote_id_and_uri(self):
        dataset = build_dataset(remote_id="1", uri="http://ex/1")

        assert set(DatasetReference.index_keys(dataset)) == {"1", "http://ex/1"}

    def test_index_keys_ignores_non_datasets(self):
        service = Dataservice(harvest={"remote_id": "s"})

        assert list(DatasetReference.index_keys(service)) == []

    def test_resolve_by_identifier_exact(self):
        d1 = build_dataset(remote_id="1")
        d2 = build_dataset(remote_id="2")
        registry = {"1": d1, "2": d2}

        targets = DatasetReference.resolve(registry, InSeriesReference(identifiers=["2"], uris=[]))

        assert targets == [d2]

    def test_resolve_endswith_fallback(self):
        d1 = build_dataset(remote_id="http://data.test.org/1")
        registry = {"http://data.test.org/1": d1}

        targets = DatasetReference.resolve(registry, InSeriesReference(identifiers=["1"], uris=[]))

        assert targets == [d1]

    def test_resolve_by_uri(self):
        d1 = build_dataset(uri="http://ex/1")
        registry = {"http://ex/1": d1}

        targets = DatasetReference.resolve(
            registry, InSeriesReference(identifiers=[], uris=["http://ex/1"])
        )

        assert targets == [d1]

    def test_resolve_by_resource_uri(self):
        """A standard resource reference (parent reachable only through its resource URI, no
        dct:identifier) resolves against the harvested parent's `harvest.uri`."""
        parent = build_dataset(uri="http://ex/api/records/parent")
        registry = {"http://ex/api/records/parent": parent}

        targets = DatasetReference.resolve(
            registry, InSeriesReference(identifiers=[], uris=["http://ex/api/records/parent"])
        )

        assert targets == [parent]

    def test_resolve_resource_uri_unknown(self):
        """An unknown resource URI yields no target (and must not fall through to the record
        reference branch, which only handles `urn:iso19139:*` URIs)."""
        parent = build_dataset(uri="http://ex/api/records/parent", record_uuid="12345")
        registry = {"http://ex/api/records/parent": parent, "12345": parent}

        targets = DatasetReference.resolve(
            registry, InSeriesReference(identifiers=[], uris=["http://ex/api/records/other"])
        )

        assert targets == []

    def test_resolve_dedups(self):
        d1 = build_dataset(remote_id="1", uri="http://ex/1")
        registry = {"1": d1, "http://ex/1": d1}

        targets = DatasetReference.resolve(
            registry, InSeriesReference(identifiers=["1"], uris=["http://ex/1"])
        )

        assert targets == [d1]

    def test_resolve_unknown_returns_empty(self):
        registry = {"1": build_dataset(remote_id="1")}

        targets = DatasetReference.resolve(
            registry, InSeriesReference(identifiers=["nope"], uris=[])
        )

        assert targets == []

    def test_resolve_by_iso19139_parent_urn(self):
        """A `urn:iso19139:parent:<uuid>` URI resolves to the dataset carrying that record UUID."""
        parent = build_dataset(remote_id="series", record_uuid="12345")
        registry = {"series": parent, "12345": parent}

        targets = DatasetReference.resolve(
            registry, InSeriesReference(identifiers=[], uris=["urn:iso19139:parent:12345"])
        )

        assert targets == [parent]

    def test_resolve_iso19139_parent_urn_unknown_uuid(self):
        """An unresolvable `urn:iso19139:parent:<uuid>` URI yields no target."""
        parent = build_dataset(remote_id="series", record_uuid="12345")
        registry = {"series": parent, "12345": parent}

        targets = DatasetReference.resolve(
            registry, InSeriesReference(identifiers=[], uris=["urn:iso19139:parent:unknown"])
        )

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

        ServesDatasetsReference.apply(type("R", (), {"dryrun": True})(), owner, [d1, d2])

        assert owner.datasets == [d1, d2]

    def test_apply_dedups_existing(self):
        d1 = build_dataset(remote_id="1")
        owner = _FakeOwner(datasets=[d1])

        ServesDatasetsReference.apply(type("R", (), {"dryrun": True})(), owner, [d1, d1])

        assert owner.datasets == [d1]

    def test_apply_live_run_saves(self):
        owner = _FakeOwner()

        ServesDatasetsReference.apply(
            type("R", (), {"dryrun": False})(), owner, [build_dataset(remote_id="1")]
        )

        assert owner.saved is True

    def test_apply_dryrun_does_not_save(self):
        owner = _FakeOwner()

        ServesDatasetsReference.apply(
            type("R", (), {"dryrun": True})(), owner, [build_dataset(remote_id="1")]
        )

        assert owner.saved is False


class InSeriesKindTest:
    def test_apply_sets_in_series_extra(self):
        child = build_dataset(remote_id="child")
        parent = build_dataset(remote_id="parent")
        parent.id = "parent_id"

        InSeriesReference.apply(type("R", (), {"dryrun": True})(), child, [parent])

        assert child.extras["dcat"]["in_series"] == "parent_id"

    def test_apply_empty_targets_is_noop(self):
        child = build_dataset(remote_id="child")

        InSeriesReference.apply(type("R", (), {"dryrun": True})(), child, [])

        assert "dcat" not in child.extras


class _FakeRef(Reference):
    predicate = URIRef("http://ex/fake")

    @classmethod
    def from_rdf(cls, node):
        return cls(identifiers=[], uris=[])

    @classmethod
    def index_keys(cls, item):
        yield item.key

    @classmethod
    def resolve(cls, registry, reference):
        return [registry[k] for k in reference.identifiers if k in registry]

    @classmethod
    def apply(cls, resolver, owner, targets):
        owner.resolved = [t.key for t in targets]


class _FakeItem:
    def __init__(self, key):
        self.key = key
        self._pending = []

    def add_pending(self, reference):
        self._pending.append(PendingReference(owner=self, reference=reference))

    def pending_references(self):
        return self._pending


class ReferenceResolverTest:
    def test_register_collects_pending_and_resolves(self):
        source = _FakeItem("service")
        source.add_pending(_FakeRef(identifiers=["d1", "d2"], uris=[]))
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
        source = _FakeItem("service")
        source.add_pending(_FakeRef(identifiers=["d1", "nope"], uris=[]))
        target1 = _FakeItem("d1")

        resolver = ReferenceResolver(dryrun=True)
        resolver.register(target1)
        resolver.register(source)
        resolver.resolve()

        assert source.resolved == ["d1"]

    def test_register_captures_record_uuid_and_resolves_urn(self):
        """The resolver registers the run-scoped record UUID and resolves `urn:iso19139:parent:`
        references against it end-to-end."""
        parent = build_dataset(remote_id="series", record_uuid="12345")
        child = build_dataset(remote_id="child")
        child.add_pending(InSeriesReference(identifiers=[], uris=["urn:iso19139:parent:12345"]))
        parent.id = "parent_id"

        resolver = ReferenceResolver(dryrun=True)
        resolver.register(parent)
        resolver.register(child)
        resolver.resolve()

        assert resolver._record_uuids == {"12345": parent}
        assert child.extras["dcat"]["in_series"] == "parent_id"

    def test_resolve_standard_resource_reference_end_to_end(self):
        """A standard (GeoNetwork-style) `dcat:inSeries` resource reference — parent reachable
        only through its resource URI — resolves end-to-end without any record UUID mapping."""
        parent = build_dataset(uri="http://ex/api/records/parent")
        child = build_dataset(remote_id="child")
        child.add_pending(InSeriesReference(identifiers=[], uris=["http://ex/api/records/parent"]))
        parent.id = "parent_id"

        resolver = ReferenceResolver(dryrun=True)
        resolver.register(parent)
        resolver.register(child)
        resolver.resolve()

        assert child.extras["dcat"]["in_series"] == "parent_id"
        assert resolver._record_uuids == {}
