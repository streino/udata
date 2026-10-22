"""Shared test helpers for the harvest reference-resolution machinery.

`build_dataset` builds a real, DB-free `Dataset` carrying the subset of harvest metadata
(`remote_id`, `uri`) and the transient record UUID (`_remote_record_id`) that the
`RelationResolver` relies on, so both the resolver orchestration tests
(`udata.tests.test_harvest`) and the RDF parsing tests
(`udata.tests.dataset.test_dataset_rdf`) can exercise the machinery without a database.
"""

from udata.core.dataset.models import Dataset


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
