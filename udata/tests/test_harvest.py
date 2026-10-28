import pytest

from udata.core.dataservices.models import Dataservice, HarvestDataserviceMetadata
from udata.core.dataset.models import Dataset, HarvestDatasetMetadata


class HarvestableTest:
    """`Harvestable.set_harvested` instantiates the metadata class of the `harvest` field when
    it is unset and never clobbers an existing value."""

    @pytest.mark.parametrize(
        "model, metadata",
        [(Dataset, HarvestDatasetMetadata), (Dataservice, HarvestDataserviceMetadata)],
    )
    def test_set_harvested_creates_the_field_metadata_class_and_is_idempotent(
        self, model, metadata
    ):
        item = model()
        item.set_harvested()
        assert isinstance(item.harvest, metadata)

        harvest = item.harvest
        item.set_harvested()
        assert item.harvest is harvest
