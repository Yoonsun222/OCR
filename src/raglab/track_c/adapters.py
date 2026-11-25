"""Project Track A and Track B evidence into mixed retrieval records."""

from raglab.contracts.evidence import EvidenceRegionV1, EvidenceRole
from raglab.track_c.models import IndexRecord


_CHART_ROLES = {EvidenceRole.SERIES, EvidenceRole.LEGEND, EvidenceRole.AXIS}


def _metadata_text(evidence: EvidenceRegionV1, key: str) -> str | None:
    value = evidence.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _searchable(
    evidence: EvidenceRegionV1,
    metadata_keys: tuple[str, ...],
    extra: tuple[str, ...] = (),
) -> str:
    values = (evidence.text, *(_metadata_text(evidence, key) for key in metadata_keys), *extra)
    return " ".join(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def from_track_a(evidence: EvidenceRegionV1, document_id: str) -> IndexRecord:
    """Index the evidence label and its explicit target relation."""
    roles = {region.region_id: region.role for region in evidence.regions}
    has_pair = any(
        relation.role is EvidenceRole.LABEL
        and roles.get(relation.target) is EvidenceRole.LABEL
        and roles.get(relation.target_region_id) is EvidenceRole.TARGET
        for relation in evidence.relations
    )
    return IndexRecord(
        evidence=evidence,
        document_id=document_id,
        channel="visual",
        searchable_text=_searchable(evidence, ("normalized_label",)),
        relation_kinds=("label_targets",) if has_pair else (),
    )


def from_track_b(evidence: EvidenceRegionV1, document_id: str) -> IndexRecord:
    """Index native observations and the chart references already present in evidence."""
    native_relations = tuple(
        relation for relation in evidence.relations if relation.target_region_id is None
    )
    source_range = next(
        (relation.target for relation in native_relations if relation.role is EvidenceRole.VALUE),
        None,
    )
    # Explicit metadata is a retrieval key. Only the evidence carries provenance.
    structured_key = (
        source_range
        or _metadata_text(evidence, "source_range")
        or evidence.native_locator.cell_range
        or _metadata_text(evidence, "label")
        or _metadata_text(evidence, "chart_title")
        or evidence.role.value
    )
    relation_text = ()
    if evidence.role in _CHART_ROLES:
        relation_text = tuple(
            value for relation in native_relations
            for value in (relation.target, relation.related) if value is not None
        )
    return IndexRecord(
        evidence=evidence,
        document_id=document_id,
        channel="structure",
        searchable_text=_searchable(evidence, ("label", "unit", "chart_title", "chart_part"), relation_text),
        structured_key=structured_key,
        # A series observation carries the cached legend. Its value range is unresolved.
        structured_value=None if evidence.role is EvidenceRole.SERIES else evidence.text,
        unit=_metadata_text(evidence, "unit"),
        relation_kinds=tuple(sorted({relation.role.value for relation in native_relations})),
    )
