import logging

from mongoengine import EmbeddedDocument
from mongoengine.fields import DateTimeField, StringField

from udata.api_fields import field
from udata.core.owned import Owned
from udata.mongo.url_field import URLField
from udata.references import PendingReference, ReferenceKind

log = logging.getLogger(__name__)


class HarvestMetadata(EmbeddedDocument):
    meta = {"abstract": True}

    backend = field(StringField())

    domain = field(StringField())

    source_id = field(StringField())
    source_url = field(URLField())

    # TODO: move `uri` field here (need to converge on type)

    remote_id = field(StringField())
    remote_url = field(URLField())

    created_at = field(
        DateTimeField(), description="Date of creation as provided by the harvested catalog"
    )
    issued_at = field(
        DateTimeField(), description="Date of release as provided by the harvested catalog"
    )
    modified_at = field(
        DateTimeField(),
        description="Date of last modification as provided by the harvested catalog",
    )

    last_update = field(DateTimeField(), description="Date of last harvest")

    archived_at = field(DateTimeField())
    archived_reason = field(StringField())


class Harvestable(Owned):
    """Base class for the models that can be harvested."""

    # Type annotation only: each concrete model declares its own `harvest` field.
    harvest: HarvestMetadata

    harvest_metadata_class: type[HarvestMetadata] = HarvestMetadata

    def set_harvested(self):
        if not self.harvest:
            self.harvest = self.harvest_metadata_class()

    def add_pending(self, reference: ReferenceKind, data) -> None:
        log.info(f"REFS: add_pending: kind={reference.name}, {data=}")
        pending = PendingReference(owner=self, kind=reference, data=data)
        object.__setattr__(self, "_pending_references", self.pending_references() + [pending])

    def pending_references(self):
        try:
            return object.__getattribute__(self, "_pending_references")
        except AttributeError:
            return []
