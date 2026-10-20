"""Unit tests for the harvest reference resolution machinery.

These tests exercise `udata.core.harvest`: the `Relation` strategy base, the `PendingRelation`
value produced by `from_rdf`, and the `RelationResolver`. A lightweight `FakeRelation` subclass
drives the generic resolver orchestration (parse -> register -> resolve -> apply) without
coupling to a concrete relation. Real `Dataset` instances (via `build_dataset`) are used as the
harvested items so that the real `RelationResolver.register` is exercised. Two end-to-end tests
also drive the resolver with the real `InSeriesRelation` to cover run-scoped record-UUID
resolution. No database is required.
"""

from typing import cast

from rdflib import Graph, Namespace, URIRef

from udata.core.dataset.models import Dataset
from udata.core.dataset.rdf import InSeriesRelation
from udata.core.harvest import ReferenceValue, Relation, RelationResolver

DCAT = Namespace("http://www.w3.org/ns/dcat#")


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


def service_node():
    return Graph().resource(URIRef("http://ex/service"))


class FakeRelation(Relation[Dataset]):
    """A minimal `Relation` strategy used to drive the generic resolver orchestration."""

    predicate = URIRef("http://ex/fake")
    # Default reference values. A subclass can override `refs` to vary what `from_rdf` collects.
    refs: tuple[str, ...] = ("d1", "d2")

    @classmethod
    def identifiers_from_rdf(cls, node):
        # Test-only: returns fixed references without reading the RDF node.
        return [ReferenceValue(r, endswith_allowed=True) for r in cls.refs]

    @classmethod
    def apply(cls, owner, targets):
        cast(Dataset, owner).extras["fake"] = [t.harvest.remote_id for t in targets]


class FakeEmptyRelation(FakeRelation):
    """A `Relation` whose reference collection is empty (no usable reference)."""

    refs = ()


class FakeUnresolvedRelation(FakeRelation):
    """A `Relation` whose references never match any registered item."""

    refs = ("missing1", "missing2")


class RelationResolverTest:
    def test_accumulates_multiple_pending_relations(self):
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        node = service_node()
        resolver.register(source, [FakeRelation.from_rdf(node), FakeRelation.from_rdf(node)])

        assert len(resolver._pending[source]) == 2

    def test_groups_pending_by_owner(self):
        resolver = RelationResolver(dryrun=True)
        source1 = build_dataset(remote_id="service1")
        source2 = build_dataset(remote_id="service2")
        node = service_node()

        resolver.register(source1, [FakeRelation.from_rdf(node)])
        resolver.register(source2, [FakeRelation.from_rdf(node)])

        assert len(resolver._pending) == 2
        assert resolver._pending[source1] is not resolver._pending[source2]

    def test_resolve_saves_each_mutated_owner_once(self, mocker):
        resolver = RelationResolver(dryrun=False)
        source = build_dataset(remote_id="service")
        save = mocker.patch.object(source, "save")
        target1 = build_dataset(remote_id="d1")
        target2 = build_dataset(remote_id="d2")
        node = service_node()
        resolver.register(source, [FakeRelation.from_rdf(node), FakeRelation.from_rdf(node)])

        for item in (target1, target2, source):
            resolver.register(item)
        resolver.resolve()

        assert source.extras["fake"] == ["d1", "d2"]
        save.assert_called_once()

    def test_resolve_dryrun_does_not_save(self, mocker):
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        save = mocker.patch.object(source, "save")
        target = build_dataset(remote_id="d1")
        node = service_node()
        resolver.register(source, [FakeRelation.from_rdf(node)])

        for item in (target, source):
            resolver.register(item)
        resolver.resolve()

        assert source.extras["fake"] == ["d1"]
        save.assert_not_called()

    def test_resolve_unresolved_owner_is_not_saved(self, mocker):
        resolver = RelationResolver(dryrun=False)
        source = build_dataset(remote_id="service")
        save = mocker.patch.object(source, "save")
        node = service_node()
        resolver.register(source, [FakeRelation.from_rdf(node)])

        resolver.register(source)
        resolver.resolve()

        assert "fake" not in source.extras
        save.assert_not_called()

    def test_from_rdf_empty_reference_is_not_collected(self):
        """An empty reference collection yields `None` from `from_rdf` and nothing to resolve."""
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        node = service_node()

        relation = FakeEmptyRelation.from_rdf(node)
        assert relation is None
        resolver.register(source)

        assert resolver._pending == {}

    def test_pending_relation_groups_under_owner_on_register(self):
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        node = service_node()
        relation = FakeRelation.from_rdf(node)

        resolver.register(source, [relation])

        assert resolver._pending[source][0] is relation

    def test_resolve_saves_only_owners_with_resolved_targets(self, mocker):
        resolver = RelationResolver(dryrun=False)
        resolved = build_dataset(remote_id="service1")
        unresolved = build_dataset(remote_id="service2")
        save_resolved = mocker.patch.object(resolved, "save")
        save_unresolved = mocker.patch.object(unresolved, "save")
        target = build_dataset(remote_id="d1")
        node = service_node()

        resolver.register(resolved, [FakeRelation.from_rdf(node)])
        resolver.register(unresolved, [FakeUnresolvedRelation.from_rdf(node)])

        for item in (target, resolved, unresolved):
            resolver.register(item)
        resolver.resolve()

        assert resolved.extras["fake"] == ["d1"]
        save_resolved.assert_called_once()
        assert "fake" not in unresolved.extras
        save_unresolved.assert_not_called()

    def test_resolve_in_series_keeps_only_first_parent(self):
        """A child may reference several parents (`dcat:inSeries` is 0..n in DCAT), but the
        applied behavior is 0..1: only the first matching parent is recorded."""
        parent1 = build_dataset(uri="http://ex/parents/1")
        parent2 = build_dataset(uri="http://ex/parents/2")
        child = build_dataset(remote_id="child")
        graph = Graph()
        graph.add((URIRef("http://ex/child"), DCAT.inSeries, URIRef("http://ex/parents/1")))
        graph.add((URIRef("http://ex/child"), DCAT.inSeries, URIRef("http://ex/parents/2")))

        resolver = RelationResolver(dryrun=True)
        resolver.register(
            child, [InSeriesRelation.from_rdf(graph.resource(URIRef("http://ex/child")))]
        )
        for item in (parent1, parent2, child):
            resolver.register(item)
        resolver.resolve()

        assert child.extras["dcat"]["in_series"] == str(parent1.id)

    def test_register_and_collect_resolves(self):
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        target1 = build_dataset(remote_id="d1")
        target2 = build_dataset(remote_id="d2")
        resolver.register(source, [FakeRelation.from_rdf(service_node())])

        for item in (target1, target2, source):
            resolver.register(item)
        resolver.resolve()

        assert source.extras["fake"] == ["d1", "d2"]

    def test_resolve_without_pending_is_noop(self):
        target = build_dataset(remote_id="d1")

        resolver = RelationResolver(dryrun=True)
        resolver.register(target)
        resolver.resolve()

        assert "fake" not in target.extras

    def test_resolve_unknown_target_is_skipped(self):
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        target1 = build_dataset(remote_id="d1")
        resolver.register(source, [FakeRelation.from_rdf(service_node())])

        resolver.register(target1)
        resolver.register(source)
        resolver.resolve()

        assert source.extras["fake"] == ["d1"]

    def test_register_captures_record_uuid_and_resolves_urn(self):
        """The resolver registers the run-scoped record UUID and resolves `urn:iso19139:parent:`
        references against it end-to-end."""
        parent = build_dataset(remote_id="series", record_uuid="12345")
        child = build_dataset(remote_id="child")
        graph = Graph()
        graph.add((URIRef("http://ex/child"), DCAT.inSeries, URIRef("urn:iso19139:parent:12345")))

        resolver = RelationResolver(dryrun=True)
        resolver.register(
            child, [InSeriesRelation.from_rdf(graph.resource(URIRef("http://ex/child")))]
        )
        resolver.register(parent)
        resolver.register(child)
        resolver.resolve()

        resource_registry, _, record_registry = resolver._build_registries(resolver._items)
        assert record_registry == {"12345": parent}
        assert child.extras["dcat"]["in_series"] == str(parent.id)

    def test_resolve_standard_resource_reference_end_to_end(self):
        """A standard (GeoNetwork-style) `dcat:inSeries` resource reference — the parent is
        reachable only through its resource URI — resolves end-to-end without any record-UUID
        mapping."""
        parent = build_dataset(uri="http://ex/api/records/parent")
        child = build_dataset(remote_id="child")
        graph = Graph()
        graph.add(
            (URIRef("http://ex/child"), DCAT.inSeries, URIRef("http://ex/api/records/parent"))
        )

        resolver = RelationResolver(dryrun=True)
        resolver.register(
            child, [InSeriesRelation.from_rdf(graph.resource(URIRef("http://ex/child")))]
        )
        resolver.register(parent)
        resolver.register(child)
        resolver.resolve()

        assert child.extras["dcat"]["in_series"] == str(parent.id)
