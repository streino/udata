from xml.etree.ElementTree import XML

import pytest
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
from udata.core.dataset.models import HarvestDatasetMetadata
from udata.core.harvest import RelationResolver
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
    def test_minimal(self):
        node = BNode()
        g = Graph()

        title = faker.sentence()
        g.add((node, RDF.type, DCAT.DataService))
        g.add((node, DCT.title, Literal(title)))

        dataservice, _ = dataservice_from_rdf(g, Dataservice(), node)
        dataservice.validate()

        assert isinstance(dataservice, Dataservice)
        assert dataservice.title == title

    def test_returns_pending_relation_value(self):
        node = BNode()
        dataset = URIRef("http://ex/api/records/dataset")
        g = Graph()

        title = faker.sentence()
        g.add((node, RDF.type, DCAT.DataService))
        g.add((node, DCT.title, Literal(title)))
        g.add((node, DCAT.servesDataset, dataset))

        dataservice, relations = dataservice_from_rdf(g, Dataservice(), node)
        assert dataservice.datasets == []
        assert [r.relation_cls.__name__ for r in relations] == ["ServesDatasetRelation"]


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


class ServesDatasetRelationTest(PytestOnlyDBTestCase):
    def test_apply_appends_targets(self, mocker):
        owner = DataserviceFactory()
        mocker.patch.object(owner, "save")
        d1 = DatasetFactory()
        d2 = DatasetFactory()

        ServesDatasetRelation.apply(owner, [d1, d2])

        assert list(owner.datasets) == [d1, d2]
        owner.save.assert_not_called()

    def test_apply_dedups_existing(self, mocker):
        d1 = DatasetFactory()
        owner = DataserviceFactory(datasets=[d1])
        mocker.patch.object(owner, "save")

        ServesDatasetRelation.apply(owner, [d1, d1])

        assert list(owner.datasets) == [d1]
        owner.save.assert_not_called()

    def test_apply_live_run_does_not_save(self, mocker):
        owner = DataserviceFactory()
        mocker.patch.object(owner, "save")
        d1 = DatasetFactory()

        ServesDatasetRelation.apply(owner, [d1])

        owner.save.assert_not_called()

    def test_apply_dryrun_does_not_save(self, mocker):
        owner = DataserviceFactory()
        mocker.patch.object(owner, "save")
        d1 = DatasetFactory()

        ServesDatasetRelation.apply(owner, [d1])

        owner.save.assert_not_called()

    @staticmethod
    def _resolvable(remote_id=None, uri=None):
        """A real, DB-backed `Dataset` carrying resolvable harvest identifiers."""
        harvest = HarvestDatasetMetadata(remote_id=remote_id, uri=uri)
        dataset = DatasetFactory(harvest=harvest)
        dataset.save()
        return dataset

    def _serves_graph(self, refs):
        """A graph whose dataservice node `dcat:servesDataset`-references each `ref`."""
        graph = Graph()
        node = URIRef("http://ex/service")
        for ref in refs:
            graph.add((node, DCAT.servesDataset, URIRef(ref)))
        return graph, node

    def test_resolve_serves_dataset_by_uri_end_to_end(self, mocker):
        """A dataservice `dcat:servesDataset`-referencing two datasets by their resource URI
        resolves them run-scoped and appends both to `dataservice.datasets` (0..n collection)."""
        owner = DataserviceFactory()
        save = mocker.patch.object(owner, "save")
        d1 = self._resolvable(uri="http://ex/d1")
        d2 = self._resolvable(uri="http://ex/d2")
        graph, node = self._serves_graph(["http://ex/d1", "http://ex/d2"])

        resolver = RelationResolver(dryrun=False)
        resolver.register(owner, [ServesDatasetRelation.from_rdf(graph.resource(node))])
        for item in (d1, d2, owner):
            resolver.register(item)
        resolver.resolve_all()

        assert list(owner.datasets) == [d1, d2]
        save.assert_called_once()

    def test_resolve_serves_dataset_by_remote_id_end_to_end(self, mocker):
        """The same run-scoped resolution also works when datasets are referenced by their
        `dct:identifier` (collected as an exact match over `harvest.remote_id`)."""
        owner = DataserviceFactory()
        mocker.patch.object(owner, "save")
        d1 = self._resolvable(remote_id="1", uri="http://ex/d1")
        d2 = self._resolvable(remote_id="2", uri="http://ex/d2")
        graph = Graph()
        node = URIRef("http://ex/service")
        for dataset, remote_id in ((d1, "1"), (d2, "2")):
            graph.add((node, DCAT.servesDataset, URIRef(dataset.harvest.remote_id)))

        resolver = RelationResolver(dryrun=True)
        resolver.register(owner, [ServesDatasetRelation.from_rdf(graph.resource(node))])
        for item in (d1, d2, owner):
            resolver.register(item)
        resolver.resolve_all()

        assert list(owner.datasets) == [d1, d2]

    def test_resolve_serves_dataset_skips_unresolved(self, mocker):
        """A served dataset that is not part of the run is ignored; only resolvable ones are
        attached (mirrors `test_harvest_dataservices_serving_datasets_referenced_by_uri`)."""
        owner = DataserviceFactory()
        mocker.patch.object(owner, "save")
        d1 = self._resolvable(uri="http://ex/d1")
        graph, node = self._serves_graph(["http://ex/d1", "http://ex/outside"])

        resolver = RelationResolver(dryrun=True)
        resolver.register(owner, [ServesDatasetRelation.from_rdf(graph.resource(node))])
        for item in (d1, owner):
            resolver.register(item)
        resolver.resolve_all()

        assert list(owner.datasets) == [d1]

    def test_resolve_dryrun_does_not_save_owner(self, mocker):
        """A dry-run resolution applies the relation in memory but never persists the owner."""
        owner = DataserviceFactory()
        save = mocker.patch.object(owner, "save")
        d1 = self._resolvable(uri="http://ex/d1")
        graph, node = self._serves_graph(["http://ex/d1"])

        resolver = RelationResolver(dryrun=True)
        resolver.register(owner, [ServesDatasetRelation.from_rdf(graph.resource(node))])
        for item in (d1, owner):
            resolver.register(item)
        resolver.resolve_all()

        assert list(owner.datasets) == [d1]
        save.assert_not_called()
