# RDF

udata has built-in [RDF][] support allowing it to both expose and harvest RDF metadata.
It uses the [Data Catalog Vocabulary][dcat] (or [DCAT][]) as base vocabulary.

## Endpoints

udata exposes instance metadata through different RDF endpoints
and tries to follow some best practices.

*All relative URLs are relative to the API root of the udata instance,
ie. `https://www.data.gouv.fr/api/1/`*

### Content Negotiation

The following formats are supported (default in bold):

| Format             | Extension        | MIME type                                 |
|--------------------|------------------|-------------------------------------------|
| [RDF/XML][rdf-xml] | **xml**, rdf     | **application/rdf+xml**, application/xml  |
| [Turtle][]         | **ttl**          | **text/turtle**, application/x-turtle     |
| [Notation3][n3]    | **n3**           | **text/n3**                               |
| [JSON-LD][]        | **jsonld**, json | **application/ld+json**, application/json |
| [N-Triples][]      | **nt**           | **application/n-triples**                 |
| [TriG][]           | **trig**         | **application/trig**                      |

Each endpoint is available through a generic URL which performs content negotiation
and redirects to a set of format specific URLs.
The default format is RDF/XML.


### Organization

Organizations are available through the following URL:

    /organizations/{id}/catalog

where `id` is the organization's identifier or slug on the udata instance.

This URL performs content negotiation and redirects to:

    /organizations/{id}/catalog.{format}

It is exposed as a [DCAT Catalog][dcat-catalog] and a [Hydra Collection][hydra-collection]
This allows pagination through the `hydra:PartialCollectionView` class.

The organization's catalog embeds the organization's datasets.


### Dataset

Datasets are available through the following URL:

    /datasets/{id}/rdf

where `id` is the dataset's identifier or slug on the udata instance.

This URL performs content negotiation and redirects to:

    /datasets/{id}/rdf.{format}

Dataservices expose the same pair of endpoints under `/dataservices/{id}/rdf`.

A Dataset is exposed as a [DCAT Dataset][dcat-dataset],
a Resource as [DCAT Distribution][dcat-distribution]
and fields are mapped according to:

| Dataset           | dcat:Dataset            | notes |
|-------------------|-------------------------|-------|
| id                | dct:identifier          |       |
| title             | dct:title               |       |
| description       | dct:description         |       |
| tags              | dct:keyword             |       |
| created_at        | dct:issued              |       |
| last_modified     | dct:modified            |       |
| resources         | dcat:distribution       |       |
| temporal_coverage | dct:temporal            | Uses schema.org startDate and endDate |
| frequency         | dct:accrualPeriodicity  | Frequencies without Dublin Core equivalent are mapped to the closest one |
| license           | dct:license + dct:right | License URL in dct:license and license label in dct:right |

| Resource          | dcat:Distribution       | notes |
|-------------------|-------------------------|-------|
| id                | dct:identifier          |       |
| title             | dct:title               |       |
| description       | dct:description         |       |
| url               | dcat:downloadURL        | as URI reference |
| permanent url     | dcat:accessURL          | as URI reference |
| created_at        | dct:issued              |       |
| last_modified     | dct:modified            |       |
| format            | dct:format              |       |
| mime              | dcat:mediaType          |       |
| filesize          | dcat:byteSize          |       |
| checksum          | spdx:checksum           |       |

| TemporalCoverage | dct:PeriodOfTime |
|------------------|------------------|
| start            | schema:startDate |
| end              | schema:endDate   |

| Checksum | spdx:Checksum      |
|----------|--------------------|
| type     | spdx:algorithm     |
| value    | spdx:checksumValue |


### Catalog

The site catalog is exposed through:

    /site/catalog

and performs content negotiation to

    /site/catalog.{format}

It is exposed as a [DCAT Catalog][dcat-catalog] and a [Hydra Collection][hydra-collection]
This allows pagination through the `hydra:PartialCollectionView` class.

## Harvester

udata can harvest other RDF/DCAT enabled data portals with the [DCAT Harvester](harvesting.md#dcat).


## References

The udata rdf implementation and its harvester were created using these references:

- [Official DCAT Vocabulary documentation][dcat]
- [European DCAT-AP documentation][dcat-ap]
- [CKAN DCAT extension][ckanext-dcat]
- [Gov UK references][gov-uk-references]
- [Data.gov.uk guidance][gov-uk-guidance]

### Reference handling

When converting RDF to internal models, the `*_from_rdf` parsers (`dataset_from_rdf`,
`dataservice_from_rdf`) create or update the target model and return it together with any
declared cross‑references as pure `PendingRelation` values. Each `PendingRelation` carries the
reference's kind (`relation_cls`) and the collected target values (`identifiers`), stored as
`ReferenceValue`s. Parsing itself is pure: nothing is stored or applied — the caller decides
whether to feed these to a `RelationResolver`.

Reference targets are collected leniently by `DatasetRelation.identifiers_from_rdf`:
- a literal value → treated as an identifier;
- a `rdf:resource` URI → referenced by its `dct:identifier` when one is present, otherwise by
  the URI itself;
- an inline blank node → by its `dct:identifier`; a blank node without one is ignored with a
  warning.

Each `ReferenceValue` records `endswith_allowed` — true whenever a `dct:identifier` was present
(whatever its RDF type), making the value a candidate for the `endswith` fallback; a plain
resource URI is matched exactly only.

Resolution is two‑phase:
1. **Parsing** — `*_from_rdf` builds the model and returns a `(model, relations)` pair; no
   `save()` happens here.
2. **Resolution** — the run owner (currently the DCAT backend) calls
   `self.relations.register(item, relations)` for every processed item, then calls
   `resolve_all()` once the whole run has been walked. The resolver rebuilds lookup registries from
   all registered items (by `remote_id`, by `harvest.uri`, and the run‑scoped record UUIDs),
   resolves each owner's relations, applies them to the owner, and saves the owner once.

Inside a parser, this looks like:

```python
def dataset_from_rdf(graph, dataset=None, node=None, *, remote_url_prefix=None, dryrun=False):
    d = graph.resource(node)
    # ... populate `dataset` from the RDF node ...
    relations: list[PendingRelation] = []
    if (relation := InSeriesRelation.from_rdf(d)) is not None:
        relations.append(relation)
    return dataset, relations
```

And on the backend side, feeding them to the resolver:

```python
item, relations = from_rdf(graph, item, node, remote_url_prefix=..., dryrun=...)
self.relations.register(item, relations)
```

This logic replaces the previous manual graph traversal and guarantees a relation can point to
an item that appears later in the same catalog (consistent handling across all harvest back‑ends).

The used namespaces are:

- dct: <http://purl.org/dc/terms/>
- dcat: <http://www.w3.org/ns/dcat#>
- foaf: <http://xmlns.com/foaf/0.1/>
- hydra: <http://www.w3.org/ns/hydra/core#>
- schema: <http://schema.org/>
- spdx: <http://spdx.org/rdf/terms#>
- freq: <http://purl.org/cld/freq/>
- scv: <http://purl.org/NET/scovo#>

[rdf]: https://www.w3.org/RDF/
[rdf-xml]: https://www.w3.org/TR/rdf-syntax-grammar/
[turtle]: https://www.w3.org/TR/turtle/
[n3]: https://www.w3.org/TeamSubmission/n3/
[n-triples]: https://www.w3.org/TR/n-triples/
[trig]: https://www.w3.org/TR/trig/
[json-ld]: https://json-ld.org/
[dcat]: https://www.w3.org/TR/vocab-dcat/
[dcat-dataset]: https://www.w3.org/TR/vocab-dcat/#Class:_Dataset
[dcat-catalog]: https://www.w3.org/TR/vocab-dcat/#Class:_Catalog
[dcat-distribution]: https://www.w3.org/TR/vocab-dcat/#Class:_Distribution
[hydra-collection]: http://www.hydra-cg.com/spec/latest/core/#collections
[dcat-ap]: https://joinup.ec.europa.eu/asset/dcat_application_profile/
[ckanext-dcat]: https://github.com/ckan/ckanext-dcat
[gov-uk-references]: http://reference.data.gov.uk/
[gov-uk-guidance]: http://guidance.data.gov.uk/
