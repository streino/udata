from typing import TYPE_CHECKING, Any, ClassVar, Self

from mongoengine import EmbeddedDocument
from mongoengine.fields import DateTimeField, StringField

from udata.api_fields import field
from udata.core.owned import Owned
from udata.mongo.url_field import URLField


class HarvestMetadata(EmbeddedDocument):
    meta = {"abstract": True}

    backend = field(StringField())

    domain = field(StringField())

    source_id = field(StringField())
    source_url = field(URLField())

    # Type annotation only: Concrete subclasses MUST declare the actual field since it can vary.
    # TODO: fix concrete types inconsitency and move field() declaration here
    uri: str | None

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

    # Type annotation only: Concrete model MUST declare the actual field, as `EmbeddedDocumentField`
    # of a concrete `HarvestMetadata` subclass.
    harvest: HarvestMetadata

    # Identifier of the source metadata record.
    # Currently not persisted, but required to resolve custom "urn:iso19139:*" relations.
    # TODO: persist in `harvest` or `extras`, at least for ecologie.datagouv?
    record_id: str | None = None

    if TYPE_CHECKING:
        # Provided by the concrete `Document` at runtime.
        _fields: ClassVar[dict[str, Any]]

        def save(self, *args: Any, **kwargs: Any) -> Self: ...

    def set_harvested(self) -> None:
        if not self.harvest:
            self.harvest = type(self)._fields["harvest"].document_type()
