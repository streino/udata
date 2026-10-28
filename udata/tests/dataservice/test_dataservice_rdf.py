from xml.etree.ElementTree import XML

import pytest
from bson import ObjectId
from flask import url_for
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.resource import Resource as RdfResource

from udata.core.constants import HVD
from udata.core.dataservices.factories import DataserviceFactory, HarvestMetadataFactory
from udata.core.dataservices.models import Dataservice
from udata.core.dataservices.rdf import (
    ServesDatasetRelation,
    dataservice_from_rdf,
    dataservice_to_rdf,
)
from udata.core.dataset.factories import DatasetFactory
from udata.core.dataset.models import Dataset
from udata.harvest.relations import ItemIndex, Reference
from udata.rdf import (
    DCAT,
    DCATAP,
    DCT,
    HVD_LEGISLATION,
    TAG_TO_EU_HVD_CATEGORIES,
)
from udata.tests.api import PytestOnlyAPITestCase, PytestOnlyDBTestCase
from udata.tests.helpers import assert200, assert_redirects
from udata.utils import faker


class DataserviceToRdfTest(PytestOnlyAPITestCase):
    def test_minimal(self):
        dataservice = DataserviceFactory.build()  # Does not have an URL
        d = dataservice_to_rdf(dataservice)
        g = d.graph

        assert isinstance(d, RdfResource)

        assert g.value(d.identifier, RDF.type) == DCAT.DataService

        assert isinstance(d.identifier, BNode)
        assert d.value(DCT.identifier) == Literal(dataservice.id)
        assert d.value(DCT.title) == Literal(dataservice.title)

        # no harvest extras, fallback to internal values for dates
        assert d.value(DCT.created) == Literal(dataservice.created_at)
        assert d.value(DCT.issued) == Literal(dataservice.created_at)
        assert d.value(DCT.modified) == Literal(dataservice.metadata_modified_at)

    def test_harvested_dates(self):
        dataservice = DataserviceFactory(harvest=HarvestMetadataFactory())
        d = dataservice_to_rdf(dataservice)
        assert d.value(DCT.created) == Literal(dataservice.harvest.created_at)
        assert d.value(DCT.issued) == Literal(dataservice.harvest.issued_at)
        assert d.value(DCT.modified) == Literal(dataservice.metadata_modified_at)

    def test_hvd_dataservice(self):
        """Test that a dataservice tagged hvd has appropriate DCAT-AP HVD properties"""
        dataservice = DataserviceFactory(tags=["hvd", "mobilite", "test"])
        dataservice.add_badge(HVD)
        d = dataservice_to_rdf(dataservice)

        assert d.value(DCATAP.applicableLegislation).identifier == URIRef(HVD_LEGISLATION)
        assert d.value(DCATAP.hvdCategory).identifier == URIRef(
            TAG_TO_EU_HVD_CATEGORIES["mobilite"]
        )
        for distrib in d.objects(DCAT.distribution):
            assert distrib.value(DCATAP.applicableLegislation).identifier == URIRef(HVD_LEGISLATION)

    def test_hvd_dataservice_with_hvd_datasets(self):
        """Test that a dataservice tagged hvd has its datasets' HVD categories"""
        dataset = DatasetFactory(tags=["meteorologiques"])
        dataset_hvd_1 = DatasetFactory(tags=["hvd", "statistiques", "not-a-hvd-category"])
        dataset_hvd_2 = DatasetFactory(
            tags=["hvd", "statistiques", "mobilite", "geospatiales", "not-a-hvd-category"]
        )
        dataset_hvd_1.add_badge(HVD)
        dataset_hvd_2.add_badge(HVD)
        dataservice = DataserviceFactory(
            datasets=[dataset, dataset_hvd_1, dataset_hvd_2], tags=["hvd", "mobilite", "test"]
        )
        dataservice.add_badge(HVD)
        d = dataservice_to_rdf(dataservice)

        assert d.value(DCATAP.applicableLegislation).identifier == URIRef(HVD_LEGISLATION)
        hvd_categories = [cat.identifier for cat in d.objects(DCATAP.hvdCategory)]
        assert len(hvd_categories) == 3
        assert URIRef(TAG_TO_EU_HVD_CATEGORIES["mobilite"]) in hvd_categories
        assert URIRef(TAG_TO_EU_HVD_CATEGORIES["geospatiales"]) in hvd_categories
        assert URIRef(TAG_TO_EU_HVD_CATEGORIES["statistiques"]) in hvd_categories
        assert URIRef(TAG_TO_EU_HVD_CATEGORIES["meteorologiques"]) not in hvd_categories
        for distrib in d.objects(DCAT.distribution):
            assert distrib.value(DCATAP.applicableLegislation).identifier == URIRef(HVD_LEGISLATION)


class DataserviceFromRdfTest(PytestOnlyDBTestCase):
    """`dataservice_from_rdf` is the single RDF conversion point: besides populating the
    dataservice, it records what a harvest needs to link it with the other items of the run."""

    def service_graph(self, *serves_dataset_objects) -> tuple[Graph, BNode]:
        node = BNode()
        g = Graph()
        g.add((node, RDF.type, DCAT.DataService))
        g.add((node, DCT.title, Literal(faker.sentence())))
        for obj in serves_dataset_objects:
            g.add((node, DCAT.servesDataset, obj))
        return g, node

    def test_serves_dataset_is_recorded_not_resolved(self):
        """Besides populating the dataservice, the parser records the references it declares and
        its source record id, for the harvest to resolve them once all items are known."""
        g, node = self.service_graph(URIRef("http://ex/api/records/dataset"))

        dataservice, (relation,) = dataservice_from_rdf(g, Dataservice(), node)

        assert dataservice.datasets == []
        assert isinstance(relation, ServesDatasetRelation)
        assert relation.references == [Reference(uri="http://ex/api/records/dataset")]
        assert dataservice.record_id is None

    def test_no_reference_no_relation(self):
        """A dataservice that declares no `dcat:servesDataset` returns no relation: it takes no
        part in the relation resolution."""
        g, node = self.service_graph()

        dataservice, relations = dataservice_from_rdf(g, Dataservice(), node)

        assert dataservice.record_id is None
        assert relations == []


class DataserviceRdfViewsTest(PytestOnlyAPITestCase):
    def test_rdf_default_to_jsonld(self, client):
        dataservice = DataserviceFactory()
        expected = url_for("api.dataservice_rdf_format", dataservice=dataservice.id, _format="json")
        response = client.get(url_for("api.dataservice_rdf", dataservice=dataservice))
        assert_redirects(response, expected)

    def test_rdf_perform_content_negociation(self, client):
        dataservice = DataserviceFactory()
        expected = url_for("api.dataservice_rdf_format", dataservice=dataservice.id, _format="xml")
        url = url_for("api.dataservice_rdf", dataservice=dataservice)
        headers = {"accept": "application/xml"}
        response = client.get(url, headers=headers)
        assert_redirects(response, expected)

    def test_rdf_perform_content_negociation_response(self, client):
        """Check we have valid XML as output"""
        dataservice = DataserviceFactory()
        url = url_for("api.dataservice_rdf", dataservice=dataservice)
        headers = {"accept": "application/xml"}
        response = client.get(url, headers=headers, follow_redirects=True)
        element = XML(response.data)
        assert element.tag == "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"

    def test_dataservice_rdf_json_ld(self, client):
        dataservice = DataserviceFactory()
        for fmt in "json", "jsonld":
            url = url_for("api.dataservice_rdf_format", dataservice=dataservice, _format=fmt)
            response = client.get(url, headers={"Accept": "application/ld+json"})
            assert200(response)
            assert response.content_type == "application/ld+json"
            assert response.json["@context"]["@vocab"] == "http://www.w3.org/ns/dcat#"

    @pytest.mark.parametrize(
        "fmt,mime",
        [
            ("n3", "text/n3"),
            ("nt", "application/n-triples"),
            ("ttl", "application/x-turtle"),
            ("xml", "application/rdf+xml"),
            ("rdf", "application/rdf+xml"),
            ("owl", "application/rdf+xml"),
            ("trig", "application/trig"),
        ],
    )
    def test_dataservice_rdf_formats(self, client, fmt, mime):
        dataservice = DataserviceFactory()
        url = url_for("api.dataservice_rdf_format", dataservice=dataservice, _format=fmt)
        response = client.get(url, headers={"Accept": mime})
        assert200(response)
        assert response.content_type == mime


class ServesDatasetRelationTest:
    """Unit tests of `ServesDatasetRelation`: match (including the suffix fallback) and apply."""

    def match(self, *items: Dataset, reference: Reference) -> Dataset | None:
        return ServesDatasetRelation([reference]).match(reference, ItemIndex(items))

    def apply(self, owner, *targets):
        return ServesDatasetRelation((Reference(identifier="d"),)).apply(owner, targets)

    def test_resolves_by_exact_id(self):
        dataset = DatasetFactory.build(harvest={"remote_id": "1"})

        assert self.match(dataset, reference=Reference(identifier="1")) is dataset

    def test_resolves_by_exact_uri(self):
        dataset = DatasetFactory.build(harvest={"uri": "http://data.test.org/datasets/1"})

        assert (
            self.match(dataset, reference=Reference(uri="http://data.test.org/datasets/1"))
            is dataset
        )

    def test_resolved_uri_address_suppresses_the_suffix_fallback(self):
        """The suffix scan only runs when both exact lookups failed."""
        dataset = DatasetFactory.build(harvest={"uri": "http://data.test.org/datasets/1"})
        suffixable = DatasetFactory.build(
            harvest={"remote_id": "http://x/1"}
        )  # identifier "1" would suffix-match it

        reference = Reference(identifier="1", uri="http://data.test.org/datasets/1")

        assert self.match(suffixable, dataset, reference=reference) is dataset

    def test_resolves_by_suffix(self):
        dataset = DatasetFactory.build(harvest={"remote_id": "http://data.test.org/1"})

        assert self.match(dataset, reference=Reference(identifier="1")) is dataset

    def test_suffix_requires_an_identifier(self):
        """A node-URI reference is never suffix-matched."""
        dataset = DatasetFactory.build(harvest={"remote_id": "http://data.test.org/1"})

        assert self.match(dataset, reference=Reference(uri="1")) is None

    def test_suffix_requires_a_boundary(self):
        dataset = DatasetFactory.build(harvest={"remote_id": "http://data.test.org/11"})

        assert self.match(dataset, reference=Reference(identifier="1")) is None

    def test_ambiguous_suffix_takes_the_first_candidate(self):
        """On several candidates, the first indexed one wins (`main` took the first match too)."""
        d1 = DatasetFactory.build(harvest={"remote_id": "http://a.org/1"})
        d2 = DatasetFactory.build(harvest={"remote_id": "http://b.org/1"})

        assert self.match(d1, d2, reference=Reference(identifier="1")) is d1

    def test_suffix_only_considers_the_expected_type(self):
        service = DataserviceFactory.build(harvest={"remote_id": "http://data.test.org/1"})

        assert self.match(service, reference=Reference(identifier="1")) is None

    def test_appends_targets(self):
        # The datasets need a pk: `LazyReference` membership degenerates on pk-less documents.
        owner, d1, d2 = (
            DataserviceFactory.build(),
            DatasetFactory.build(id=ObjectId()),
            DatasetFactory.build(id=ObjectId()),
        )

        assert self.apply(owner, d1, d2) is True
        assert list(owner.datasets) == [d1, d2]

    def test_keeps_datasets_already_attached(self):
        d1, d2 = DatasetFactory.build(id=ObjectId()), DatasetFactory.build(id=ObjectId())
        owner = DataserviceFactory.build()
        owner.datasets = [d1]

        assert self.apply(owner, d2) is True
        assert list(owner.datasets) == [d1, d2]

    def test_already_attached_is_unchanged(self):
        d1 = DatasetFactory.build(id=ObjectId())
        owner = DataserviceFactory.build()
        owner.datasets = [d1]

        assert self.apply(owner, d1) is False

    def test_nothing_to_attach_is_unchanged(self):
        assert self.apply(DataserviceFactory.build()) is False
