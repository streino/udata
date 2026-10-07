"""Reference resolution for harvested items.

The DcatBackend walks several RDF pages and a given record may reference another one
(a dataservice `dcat:servesDataset`, a dataset `dcat:inSeries`, ...) that is defined on a
different page of the same harvest. Those cross-references cannot be resolved while parsing a
single record, so they are collected as pending references and resolved in a second pass over
everything that was seen during the run.

This module is a dependency-free leaf: it only depends on `rdflib` and the standard library, so
both the core models (``udata/core/*/models.py``) and the harvest backends can import it without
creating import cycles.
"""

import logging
from dataclasses import dataclass
from typing import Any, Hashable, Iterable


@dataclass
class Reference:
    """Identifiers and URIs collected from the objects of an RDF predicate."""

    identifiers: list[str]
    uris: list[str]


log = logging.getLogger(__name__)


def references_from_rdf(node, predicate) -> Reference:
    """
    Collect the URIs of the objects of `predicate` on an rdflib `Resource`.

    Only `rdf:resource` URIs are processed. Inline `dcat:Dataset` elements (e.g. with
    a `dct:identifier` but no `rdf:resource`) are ignored and a warning is emitted.
    """
    # Import DCT to detect inline identifiers.
    from rdflib import BNode

    from udata.rdf import DCT

    identifiers: list[str] = []
    uris: list[str] = []

    for obj in node.objects(predicate):
        # ``obj`` is a ``rdflib.resource.Resource``. Its ``identifier`` attribute holds the actual node.
        identifier_node = getattr(obj, "identifier", None)
        if identifier_node is None:
            # Unexpected case – log and skip.
            log.warning("Ignoring inline %s reference – only rdf:resource is supported", predicate)
            continue
        # If the underlying identifier is a blank node, treat as an inline definition and ignore.
        if isinstance(identifier_node, BNode):
            log.warning("Ignoring inline %s reference – only rdf:resource is supported", predicate)
            continue
        # At this point ``identifier_node`` is a URIRef.
        # Look for a ``dct:identifier`` triple on that node.
        dct_id = node.graph.value(subject=identifier_node, predicate=DCT.identifier)
        if dct_id is not None:
            identifiers.append(str(dct_id))
            continue
        # No ``dct:identifier`` – treat as a plain ``rdf:resource`` URI.
        uris.append(str(identifier_node))

    # If any plain URI references were collected, identifiers should be ignored.
    if uris:
        identifiers = []

    return Reference(identifiers=identifiers, uris=uris)


class ReferenceKind:
    """
    Resolution logic for a given type of pending reference.

    A kind is stateless: it knows how to index the seen items (``index_keys``), how to turn a
    pending reference's data into a list of target items (``resolve``) and how to apply those
    targets to the owning item (``apply``). Instances are held as values of a model's nested
    ``Reference`` enum, so each model declares the kinds it needs.
    """

    name: str = ""

    def index_keys(self, item) -> Iterable[Hashable]:
        """Yield the keys under which `item` should be findable in the registry."""
        raise NotImplementedError

    def resolve(self, registry, data) -> list[Any]:
        """Return the target items for a pending reference of this kind."""
        raise NotImplementedError

    def apply(self, resolver, owner, targets) -> None:
        """Apply the resolved `targets` to the `owner` item."""
        raise NotImplementedError


@dataclass
class PendingReference:
    """A reference collected on `owner` that is waiting for the end of the harvest run."""

    owner: Any
    kind: ReferenceKind
    data: Any
