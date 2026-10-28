"""DB-free unit tests of the harvest relation resolution (`udata.harvest.relations`) and of
the concrete relations, plus `Harvestable`."""

import logging
from collections.abc import Sequence

import pytest
from bson import ObjectId
from rdflib import Graph, URIRef

from udata.core.dataservices.models import Dataservice
from udata.core.dataset.models import Dataset
from udata.core.harvest import Harvestable
from udata.harvest.relations import (
    ItemIndex,
    Reference,
    Relation,
    RelationResolver,
)
from udata.rdf import DCAT, references_from_rdf


def build_dataset(
    remote_id: str | None = None,
    uri: str | None = None,
    *,
    record_id: str | None = None,
    saved: bool = True,
) -> Dataset:
    """A real, DB-free `Dataset` carrying exactly what the resolution machinery reads:
    `harvest.remote_id`, `harvest.uri`, the run-scoped `record_id`, and a pk (unless
    `saved=False`, modelling the dry-run where the pk is assigned late).

    Use `DatasetFactory` for realistic datasets in integration tests — its `remote_id` is an
    in-memory attribute, not `harvest.remote_id`, so it doesn't feed the resolver.
    """
    harvest = {}
    if remote_id:
        harvest["remote_id"] = remote_id
    if uri:
        harvest["uri"] = uri
    dataset = Dataset(id=ObjectId() if saved else None, harvest=harvest)
    dataset.record_id = record_id
    return dataset


def build_dataservice(remote_id: str | None = None) -> Dataservice:
    """A real, DB-free `Dataservice` carrying the harvest identifiers relations resolve on.

    Unlike `build_dataset`, no `record_id` parameter: dataservices carry none today.
    """
    dataservice = Dataservice(harvest={"remote_id": remote_id} if remote_id else {})
    return dataservice


class FakeRelation(Relation[Dataset, Dataset]):
    """A minimal concrete `Relation` for tests: records the remote ids of its resolved targets
    in the owner's `extras["fake"]` key."""

    @property
    def target_cls(self) -> type[Dataset]:
        return Dataset

    def apply(self, source: Dataset, targets: Sequence[Dataset]) -> bool:
        if not targets:
            return False
        ids = [t.harvest.remote_id for t in targets]
        if source.extras.get("fake") == ids:
            return False
        source.extras["fake"] = ids
        return True


class ReferenceTest:
    @pytest.mark.parametrize(
        "kwargs",
        [{"identifier": "x"}, {"uri": "y"}, {"identifier": "x", "uri": "y"}],
    )
    def test_create_valid(self, kwargs):
        ref = Reference(**kwargs)
        assert ref.identifier == kwargs.get("identifier")
        assert ref.uri == kwargs.get("uri")

    def test_create_invalid(self):
        """`Reference` requires at least one identifier."""
        with pytest.raises(ValueError, match="at least one"):
            Reference()


class ItemIndexTest:
    def test_first_registered_wins_on_duplicate_keys(self):
        """First indexed item wins, matching the order of `job.items`."""
        first = build_dataset(remote_id="dup", uri="http://ex/dup", record_id="rec")
        second = build_dataset(remote_id="dup", uri="http://ex/dup", record_id="rec")
        index = ItemIndex([first, second])

        assert index.remote_ids["dup"] is first
        assert index.uris["http://ex/dup"] is first
        assert index.record_ids["rec"] is first

    def test_items_are_only_indexed_under_the_identifiers_they_carry(self):
        dataset = build_dataset(remote_id="1")

        index = ItemIndex([dataset])

        assert index.remote_ids == {"1": dataset}
        assert index.uris == {}
        assert index.record_ids == {}


class RelationTest:
    def match(self, reference: Reference, items: Sequence[Harvestable]) -> Dataset | None:
        return FakeRelation([reference]).match(reference, ItemIndex(items))

    def test_exact_match_on_remote_id(self):
        d1, d2 = build_dataset(remote_id="1"), build_dataset(remote_id="2")

        assert self.match(Reference(identifier="2"), [d1, d2]) is d2

    def test_exact_match_on_uri(self):
        dataset = build_dataset(uri="http://ex/1")

        assert self.match(Reference(uri="http://ex/1"), [dataset]) is dataset

    def test_identifier_address_takes_precedence_over_the_uri_address(self):
        """A reference carrying both addresses resolves through its identifier only: `main`
        attached a single dataset per referenced object."""
        by_remote_id = build_dataset(remote_id="x")
        by_uri = build_dataset(uri="x")

        reference = Reference(identifier="x", uri="x")

        assert self.match(reference, [by_remote_id, by_uri]) is by_remote_id

    def test_identifier_reference_does_not_match_the_uri_table(self):
        by_uri = build_dataset(uri="x")

        assert self.match(Reference(identifier="x"), [by_uri]) is None

    def test_failed_identifier_lookup_falls_through_to_the_uri_address(self):
        """An identifier resolving to an unexpected item type is skipped (with a warning), and
        the URI address is still consulted."""
        service = build_dataservice(remote_id="1")
        dataset = build_dataset(uri="http://ex/1")

        reference = Reference(identifier="1", uri="http://ex/1")

        assert self.match(reference, [service, dataset]) is dataset

    def test_uri_reference_does_not_match_the_remote_id_table(self):
        by_remote_id = build_dataset(remote_id="x")

        assert self.match(Reference(uri="x"), [by_remote_id]) is None

    def test_unknown(self):
        assert self.match(Reference(identifier="nope"), [build_dataset(remote_id="1")]) is None

    def test_never_falls_back_to_suffix(self):
        """The suffix fallback is a `ServesDatasetRelation` policy, not part of the default
        match."""
        dataset = build_dataset(remote_id="http://data.test.org/1")

        assert self.match(Reference(identifier="1"), [dataset]) is None

    def test_skips_targets_of_the_wrong_type(self, caplog):
        service = build_dataservice(remote_id="1")

        with caplog.at_level(logging.WARNING):
            assert self.match(Reference(identifier="1"), [service]) is None

        assert "expected Dataset" in caplog.text

    def test_relation_is_hashable(self):
        assert hash(FakeRelation((Reference(identifier="1"),))) == hash(
            FakeRelation((Reference(identifier="1"),))
        )

    def test_resolve_skips_the_owner_itself(self):
        owner = build_dataset(remote_id="owner")
        other = build_dataset(remote_id="other")
        relation = FakeRelation((Reference(identifier="owner"), Reference(identifier="other")))

        assert relation.resolve(ItemIndex([owner, other]), source=owner) == [other]

    def test_resolve_dedups_targets(self):
        d1 = build_dataset(remote_id="1", uri="http://ex/1")
        relation = FakeRelation(
            (
                Reference(identifier="1"),
                Reference(uri="http://ex/1"),
            )
        )

        assert relation.resolve(ItemIndex([d1]), source=build_dataset()) == [d1]

    def test_resolve_skips_unknown_targets(self):
        d1 = build_dataset(remote_id="1")
        relation = FakeRelation(
            (
                Reference(identifier="nope"),
                Reference(identifier="1"),
            )
        )

        assert relation.resolve(ItemIndex([d1]), source=build_dataset()) == [d1]


class ReferencesFromRdfTest:
    """`references_from_rdf` turns each object of a `predicate` into one `Reference`, carrying
    the addresses it can be matched against."""

    def references(self, turtle: str) -> list[Reference]:
        graph = Graph().parse(data=turtle, format="turtle")
        service = graph.resource(URIRef("http://data.test.org/service"))
        return references_from_rdf(service, DCAT.servesDataset)

    def test_none(self):
        # Empty graph, so no `servesDataset`.
        assert self.references("") == []

    def test_literal_object_is_an_id_reference(self):
        refs = self.references(
            """
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/service> dcat:servesDataset "plain-id".
            """
        )

        assert refs == [Reference(identifier="plain-id")]

    def test_uriref_without_identifier_is_a_uri_reference(self):
        refs = self.references(
            """
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/service> dcat:servesDataset <http://data.test.org/dataset/1>.
            """
        )

        assert refs == [Reference(uri="http://data.test.org/dataset/1")]

    def test_uriref_with_identifier_carries_both_addresses(self):
        """A URIRef object that also carries a `dct:identifier` is addressable both by its id
        and its URI (`main`'s `remote_id == id or harvest.uri == uri`)."""
        refs = self.references(
            """
            @prefix dct: <http://purl.org/dc/terms/>.
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/dataset/1> dct:identifier "id-1".
            <http://data.test.org/service> dcat:servesDataset <http://data.test.org/dataset/1>.
            """
        )

        assert refs == [Reference(identifier="id-1", uri="http://data.test.org/dataset/1")]

    def test_uriref_with_a_uri_typed_identifier(self):
        """A URI-typed `dct:identifier` is an identifier like any other: the reference carries
        it, together with the node URI."""
        refs = self.references(
            """
            @prefix dct: <http://purl.org/dc/terms/>.
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/dataset/1> dct:identifier <urn:dataset:1>.
            <http://data.test.org/service> dcat:servesDataset <http://data.test.org/dataset/1>.
            """
        )

        assert refs == [Reference(identifier="urn:dataset:1", uri="http://data.test.org/dataset/1")]

    def test_blank_node_with_identifier_is_referenced_by_id_only(self):
        refs = self.references(
            """
            @prefix dct: <http://purl.org/dc/terms/>.
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/service> dcat:servesDataset [
                dct:identifier "id-1"
            ].
            """
        )

        assert refs == [Reference(identifier="id-1")]

    def test_blank_node_with_a_uri_identifier(self):
        """A blank node has no URI of its own: even a URI-typed identifier is the only
        address."""
        refs = self.references(
            """
            @prefix dct: <http://purl.org/dc/terms/>.
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/service> dcat:servesDataset [ dct:identifier <urn:dataset:1> ].
            """
        )

        assert refs == [Reference(identifier="urn:dataset:1")]

    def test_same_identifier_from_different_shapes_is_not_deduplicated(self):
        """A URIRef-with-identifier and a bare literal with the same value produce two distinct
        references: they collapse at the target level, not the reference level."""
        refs = self.references(
            """
            @prefix dct: <http://purl.org/dc/terms/>.
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/dataset/1> dct:identifier "id-1".
            <http://data.test.org/service> dcat:servesDataset <http://data.test.org/dataset/1>.
            <http://data.test.org/service> dcat:servesDataset "id-1".
            """
        )

        assert refs == [
            Reference(identifier="id-1", uri="http://data.test.org/dataset/1"),
            Reference(identifier="id-1"),
        ]

    def test_references_keep_the_source_order(self):
        """References are not sorted: the resolution order follows the source declaration order
        (a deliberate decision: `main` didn't sort either)."""
        refs = self.references(
            """
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/service> dcat:servesDataset "b", "a".
            """
        )

        assert refs == [
            Reference(identifier="b"),
            Reference(identifier="a"),
        ]

    def test_blank_node_without_identifier_is_ignored(self, caplog):
        refs = self.references(
            """
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/service> dcat:servesDataset [].
            """
        )

        with caplog.at_level(logging.WARNING):
            assert refs == []
        assert "No identifiers" in caplog.text

    def test_duplicate_references_are_deduplicated(self):
        refs = self.references(
            """
            @prefix dcat: <http://www.w3.org/ns/dcat#>.
            <http://data.test.org/service> dcat:servesDataset "id-1", "id-1".
            """
        )

        assert refs == [Reference(identifier="id-1")]


class RelationResolverTest:
    def resolver(self, *items) -> RelationResolver:
        resolver = RelationResolver()
        for item, relations in items:
            resolver.register(item, relations)
        return resolver

    def test_resolves_and_returns_changed_owners(self):
        owner = build_dataset(remote_id="owner")
        target = build_dataset(remote_id="d1")

        assert self.resolver(
            (owner, [FakeRelation((Reference(identifier="d1"),))]), (target, [])
        ).resolve() == [owner]
        assert owner.extras["fake"] == ["d1"]

    def test_target_may_be_registered_after_its_owner(self):
        for order in ("target first", "owner first"):
            owner = build_dataset(remote_id="owner")
            target = build_dataset(remote_id="d1")
            owner_relations = [FakeRelation((Reference(identifier="d1"),))]
            items = (
                [(target, []), (owner, owner_relations)]
                if order == "target first"
                else [(owner, owner_relations), (target, [])]
            )

            assert self.resolver(*items).resolve() == [owner], order

    def test_unchanged_owner_is_not_returned(self):
        owner = build_dataset(remote_id="owner")
        owner.extras["fake"] = ["d1"]

        assert (
            self.resolver(
                (owner, [FakeRelation((Reference(identifier="d1"),))]),
                (build_dataset(remote_id="d1"), []),
            ).resolve()
            == []
        )

    def test_owner_with_several_relations_is_returned_once(self):
        owner = build_dataset(remote_id="owner")
        items = [
            (
                owner,
                [
                    FakeRelation((Reference(identifier="d1"),)),
                    FakeRelation((Reference(identifier="d2"),)),
                ],
            ),
            (build_dataset(remote_id="d1"), []),
            (build_dataset(remote_id="d2"), []),
        ]

        # Both relations write the same key, only the last value survives: what matters here
        # is that the owner is reported once.
        assert self.resolver(*items).resolve() == [owner]

    def test_relations_registered_in_stages_are_merged(self):
        """A backend may register the same item several times (staged processing): relations
        accumulate and the item is resolved, hence reported, only once — even when its pk is
        assigned between the registrations, which changes its MongoEngine `__hash__`."""
        owner = build_dataset(remote_id="owner", saved=False)
        target = build_dataset(remote_id="d1", saved=False)
        d2 = build_dataset(remote_id="d2")
        resolver = RelationResolver()
        resolver.register(owner, [FakeRelation((Reference(identifier="d1"),))])
        resolver.register(target, [])
        owner.id = ObjectId()  # pk assigned between registrations (dry-run does it late)
        resolver.register(owner, [FakeRelation((Reference(identifier="d2"),))])
        target.id = ObjectId()
        resolver.register(d2, [])

        assert resolver.resolve() == [owner]
        # Both stages' relations were applied: the last one wins the `fake` extra.
        assert owner.extras["fake"] == ["d2"]

    def test_no_relation_nothing_to_do(self):
        assert self.resolver((build_dataset(remote_id="d1"), [])).resolve() == []

    def test_items_whose_pk_is_assigned_after_registration(self):
        """Items can be registered before their pk is known (dry-run assigns it late)."""
        owner = build_dataset(remote_id="owner", saved=False)
        target = build_dataset(remote_id="d1", saved=False)
        resolver = self.resolver(
            (owner, [FakeRelation((Reference(identifier="d1"),))]), (target, [])
        )

        owner.id = ObjectId()
        target.id = ObjectId()

        assert resolver.resolve() == [owner]

    def test_remote_id_set_after_registration(self):
        """The backend only finishes filling `harvest.remote_id` once the item is saved."""
        owner = build_dataset(remote_id="owner")
        target = build_dataset(uri="http://ex/1")
        resolver = self.resolver(
            (owner, [FakeRelation((Reference(identifier="d1"),))]), (target, [])
        )

        target.harvest.remote_id = "d1"

        assert resolver.resolve() == [owner]

    def test_unregistered_items_are_neither_targets_nor_owners(self):
        """Failed and skipped items are never registered (see `BaseBackend.process_item`): a
        relation pointing to them does not resolve, and the owner is left untouched."""
        owner = build_dataset(remote_id="owner")

        assert (
            self.resolver((owner, [FakeRelation((Reference(identifier="failed"),))])).resolve()
            == []
        )
        assert "fake" not in owner.extras  # its target "failed" was never registered
