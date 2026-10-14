"""Unit tests for the harvest reference resolution machinery.

These tests exercise `udata.core.harvest`: the `PendingRelation` base and the
`RelationResolver`. A lightweight `FakeRelation` subclass drives the generic resolver
orchestration (collect -> register -> build registry -> resolve -> apply) without coupling to a
concrete relation. Real `Dataset` instances (via `build_dataset`) are used as the harvested
items so that the real `RelationResolver.add_pending_relation` is exercised. Two end-to-end
tests also drive the resolver with the real `InSeriesRelation` to cover run-scoped record-UUID
resolution. No database is required.
"""

from typing import cast

from rdflib import Graph, Namespace, URIRef

from udata.core.dataset.models import Dataset
from udata.core.dataset.rdf import InSeriesRelation
from udata.core.harvest import PendingRelation, RelationResolver

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


class FakeRelation(PendingRelation):
    """A minimal `PendingRelation` used to drive the generic resolver orchestration."""

    predicate = URIRef("http://ex/fake")

    @classmethod
    def from_rdf(cls, node):
        # Test-only: returns a fixed reference without reading the RDF node.
        return ["d1", "d2"], []

    @classmethod
    def lookup_keys(cls, item):
        yield item.harvest.remote_id

    def resolve(self, registry):
        return [registry[k] for k in self.identifiers if k in registry]

    def apply(self, resolver, targets):
        cast(Dataset, self.owner).extras["fake"] = [t.harvest.remote_id for t in targets]


class RelationResolverTest:
    def test_accumulates_multiple_pending_relations(self):
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        node = Graph().resource(URIRef("http://ex/service"))
        resolver.add_pending_relation(source, FakeRelation, node)
        resolver.add_pending_relation(source, FakeRelation, node)

        assert len(resolver._pending[FakeRelation]) == 2

    def test_register_and_collect_resolves(self):
        resolver = RelationResolver(dryrun=True)
        source = build_dataset(remote_id="service")
        target1 = build_dataset(remote_id="d1")
        target2 = build_dataset(remote_id="d2")
        resolver.add_pending_relation(
            source, FakeRelation, Graph().resource(URIRef("http://ex/service"))
        )

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
        resolver.add_pending_relation(
            source, FakeRelation, Graph().resource(URIRef("http://ex/service"))
        )

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
        resolver.add_pending_relation(
            child, InSeriesRelation, graph.resource(URIRef("http://ex/child"))
        )
        resolver.register(parent)
        resolver.register(child)
        resolver.resolve()

        assert resolver._record_uuids == {"12345": parent}
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
        resolver.add_pending_relation(
            child, InSeriesRelation, graph.resource(URIRef("http://ex/child"))
        )
        resolver.register(parent)
        resolver.register(child)
        resolver.resolve()

        assert child.extras["dcat"]["in_series"] == str(parent.id)
