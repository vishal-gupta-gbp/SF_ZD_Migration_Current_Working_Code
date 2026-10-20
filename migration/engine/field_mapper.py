"""Database-backed field and value mapping engine."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from migration.db.models import MappingField, MappingValue

SYSTEM_ONLY_TARGETS = {
    "requester (system)",
    "organization (system)",
    "zendesk ticket external_id (import api)",
    "initial ticket public comment",
    "historical ticket comments",
    "ticket internal note/comment",
    "ticket created_at (import api)",
    "comment/ticket source author reference",
    "assignee/group (system)",
}

INTERNAL_COMMENT_FIELDS = [
    "BP_Internal_Comments__c",
    "BP_Remarks__c",
    "BP_Resolution_Notes__c",
    "BP_Case_Notes__c",
    "BP_Transcripts__c",
    "GM_Voicemail_Transcript__c",
]

ASSOCIATION_TYPE_FIELD_ID = 30064365242012
PRODUCT_FIELD_ID = 30867567540508

from migration.engine.enquiry_mapper import (
    ENQUIRY_QUERY_ISSUE_FIELD_ID,
    ENQUIRY_TEXT_FIELD_ID,
    QUERY_TEXT_FIELD_ID,
    QUERY_RELATED_ISSUE_TEXT_FIELD_ID,
    DEFAULT_ENQUIRY_TAG,
    resolve_enquiry_query_issue,
)

BRAND_PRODUCT_MAP = {
    "bag by the ocean": {
        "brand_name": "BagsByTheOcean",
        "brand_id": 30860511408796,
        "product_value": "bag_by_the_ocean",
    },
    "bagsbytheocean": {
        "brand_name": "BagsByTheOcean",
        "brand_id": 30860511408796,
        "product_value": "bag_by_the_ocean",
    },
    "bannerbuzz": {
        "brand_name": "BannerBuzz",
        "brand_id": 30860493207836,
        "product_value": "bannerbuzz",
    },
    "best of signs": {
        "brand_name": "BestofSigns",
        "brand_id": 30860512498972,
        "product_value": "best_of_signs",
    },
    "bestofsigns": {
        "brand_name": "BestofSigns",
        "brand_id": 30860512498972,
        "product_value": "best_of_signs",
    },
    "circleone": {
        "brand_name": "Circle One",
        "brand_id": 30860548756764,
        "product_value": "circleone",
    },
    "circle one": {
        "brand_name": "Circle One",
        "brand_id": 30860548756764,
        "product_value": "circleone",
    },
    "covers & all": {
        "brand_name": "Covers&All",
        "brand_id": 30860551609244,
        "product_value": "covers___all",
    },
    "covers&all": {
        "brand_name": "Covers&All",
        "brand_id": 30860551609244,
        "product_value": "covers___all",
    },
    "neon earth": {
        "brand_name": "Neon Earth",
        "brand_id": 30860552556444,
        "product_value": "neon_earth",
    },
    "patio hq": {
        "brand_name": "PatioHQ",
        "brand_id": 30860505949340,
        "product_value": "patio_hq",
    },
    "patiohq": {
        "brand_name": "PatioHQ",
        "brand_id": 30860505949340,
        "product_value": "patio_hq",
    },
    "tarps & all": {
        "brand_name": "Tarps & All",
        "brand_id": 30860556517916,
        "product_value": "tarps___all",
    },
    "tarps&all": {
        "brand_name": "Tarps & All",
        "brand_id": 30860556517916,
        "product_value": "tarps___all",
    },
    "vivyx printing": {
        "brand_name": "Vivyx Printing",
        "brand_id": 30860539169948,
        "product_value": "vivyx_printing",
    },
    "north cape": {
        "brand_name": "Group Bayport",
        "brand_id": 29179739154972,
        "product_value": "north_cape",
    },
    "giant media": {
        "brand_name": "Group Bayport",
        "brand_id": 29179739154972,
        "product_value": "giant_media",
    },
    "marketplace": {
        "brand_name": "Group Bayport",
        "brand_id": 29179739154972,
        "product_value": "marketplace",
    },
    "contact": {
        "brand_name": "Group Bayport",
        "brand_id": 29179739154972,
        "product_value": "contact",
    },
    "partners": {
        "brand_name": "Group Bayport",
        "brand_id": 29179739154972,
        "product_value": "partners",
    },
    "reports": {
        "brand_name": "Group Bayport",
        "brand_id": 29179739154972,
        "product_value": "reports",
    },
    "social media": {
        "brand_name": "Group Bayport",
        "brand_id": 29179739154972,
        "product_value": "social_media",
    },
}


def resolve_brand_and_product(prod_val: Any) -> Optional[Dict[str, Any]]:
    """Resolve Salesforce BP_Product__c value to Zendesk Brand Name, Brand ID, and Product Field Value."""
    raw = s(prod_val).strip()
    if not raw:
        return None
    key = normalize_label(raw)
    if key in BRAND_PRODUCT_MAP:
        return BRAND_PRODUCT_MAP[key]
    alphanumeric = re.sub(r"[^a-z0-9]", "", key)
    for k, v in BRAND_PRODUCT_MAP.items():
        if re.sub(r"[^a-z0-9]", "", k) == alphanumeric:
            return v
    return None



def s(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def normalize_label(val: Any) -> str:
    return s(val).strip().lower()


def safe_tag(val: Any) -> str:
    raw = s(val).strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return cleaned


def parse_checkbox(value: Any) -> Optional[bool]:
    v = s(value).lower()
    if not v:
        return None
    if v in {"true", "1", "yes", "y"}:
        return True
    if v in {"false", "0", "no", "n"}:
        return False
    return None


def parse_numeric(value: Any) -> Optional[str]:
    raw = s(value)
    if not raw:
        return None
    try:
        f = float(raw)
        if f.is_integer():
            return str(int(f))
        return str(f)
    except (ValueError, TypeError):
        return None


def sf_system_status(value: str) -> str:
    """Map Salesforce Case status to Zendesk Ticket status."""
    v = normalize_label(value)
    status_map = {
        "new": "new",
        "open": "open",
        "assigned": "open",
        "working": "open",
        "reassigned": "open",
        "escalated": "open",
        "jira escalated": "open",
        "waiting on customer": "pending",
        "waiting on department": "hold",
        "pending": "pending",
        "hold": "hold",
        "resolved": "solved",
        "solved": "solved",
        "closed": "closed",
        "discarded": "closed",
    }
    return status_map.get(v, "open")


def build_system_tags(case: Dict[str, Any]) -> List[str]:
    """Build standard migration tags and system tags from case attributes."""
    tags = ["salesforce_server_migrated", "sf_case"]

    origin = safe_tag(case.get("Origin"))
    source = safe_tag(case.get("BP_Source__c"))
    tag_value = origin or source
    if tag_value:
        tags.append(f"sf_origin_{tag_value}")
    if source and source != origin:
        tags.append(f"sf_source_{source}")

    for field_name in ("BP_Tags__c", "Tags_B2B__c"):
        raw_tags = s(case.get(field_name))
        if raw_tags:
            for part in re.split(r"[;,]", raw_tags):
                t = safe_tag(part)
                if t:
                    tags.append(t)

    for field_name in (
        "BP_Enquiry__c",
        "BP_Query__c",
        "BP_Query_B2B__c",
        "BP_Query_Related_Issue__c",
        "Query_Related_Issue_B2B__c",
    ):
        val = safe_tag(case.get(field_name))
        if val:
            tags.append(val)

    return list(dict.fromkeys(tags))


class DatabaseFieldMapper:
    """In-memory cached mapper loaded directly from database tables."""

    def __init__(self, session: Session):
        self.session = session
        self.field_rows: List[Dict[str, Any]] = []
        self.value_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.value_rows: List[Dict[str, Any]] = []
        self.grouped_target_rows: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        self.load_from_db()

    def load_from_db(self) -> None:
        db_fields = self.session.query(MappingField).all()
        for f in db_fields:
            item = {
                "Salesforce API Name": f.salesforce_api_name,
                "Salesforce Field Label": f.salesforce_label,
                "Salesforce Type": f.salesforce_type,
                "Zendesk Target Field": f.zendesk_target_field,
                "Zendesk Target Field ID": f.zendesk_target_field_id,
                "Zendesk Target Type": f.zendesk_target_type,
                "Mapping Status": f.mapping_status,
                "Transformation / Logic": f.transformation_notes,
            }
            self.field_rows.append(item)
            if f.zendesk_target_field_id:
                self.grouped_target_rows[int(f.zendesk_target_field_id)].append(item)

        db_values = self.session.query(MappingValue).all()
        for v in db_values:
            item = {
                "Salesforce API Name": v.salesforce_api_name,
                "Salesforce Value": v.salesforce_value,
                "Zendesk Value": v.zendesk_value,
                "Zendesk Tag": v.zendesk_tag,
                "Match Status": v.match_status,
            }
            self.value_rows.append(item)
            key = (v.salesforce_api_name, normalize_label(v.salesforce_value))
            if key[1] and key not in self.value_map:
                self.value_map[key] = item

    def lookup_value(self, sf_api: str, sf_value: str) -> Optional[Dict[str, Any]]:
        return self.value_map.get((sf_api, normalize_label(sf_value)))

    def mapped_zendesk_value(self, sf_api: str, value: Any) -> str:
        row = self.lookup_value(sf_api, s(value))
        if not row:
            return ""
        return s(row.get("Zendesk Value"))

    def map_custom_value(self, sf_api: str, value: Any) -> Tuple[Optional[str], Optional[str]]:
        raw = s(value)
        if not raw:
            return None, "blank"
        row = self.lookup_value(sf_api, raw)
        if not row:
            return None, "no_value_mapping"
        status = normalize_label(row.get("Match Status"))
        tag = s(row.get("Zendesk Tag"))
        if "exact label match" in status or "exact leaf match" in status or "exact option label" in status:
            if tag:
                return tag, "mapped_tag"
            zval = s(row.get("Zendesk Value"))
            if zval:
                return zval, "mapped_value"
            return None, "mapped_without_target_value"
        return None, "non_exact_or_missing_target"

    def build_custom_fields(
        self,
        case: Dict[str, Any],
        report: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build custom_fields payload list matching Zendesk target IDs."""
        result: List[Dict[str, Any]] = []
        used_ids: Set[int] = set()

        for field_id, rows in self.grouped_target_rows.items():
            if field_id in used_ids:
                continue

            chosen_row: Optional[Dict[str, Any]] = None
            chosen_value: Any = None
            mapped_value: Optional[str] = None
            mapped_reason = ""

            for row in rows:
                sf_api = s(row.get("Salesforce API Name"))
                status = normalize_label(row.get("Mapping Status"))
                target_type = normalize_label(row.get("Zendesk Target Type"))
                target_name = normalize_label(row.get("Zendesk Target Field"))

                if status == "ignore" or not sf_api or sf_api not in case:
                    continue
                if target_name in SYSTEM_ONLY_TARGETS:
                    continue
                if target_name in {"owner (custom) + assignee/group (system)", "assignee/group (system)"}:
                    continue
                if "lookup" in target_type:
                    continue
                if sf_api in {"BP_Tags__c", "Tags_B2B__c"}:
                    continue

                value = case.get(sf_api)
                if s(value) == "":
                    continue

                chosen_row = row
                chosen_value = value
                break

            if not chosen_row:
                continue

            sf_api = s(chosen_row.get("Salesforce API Name"))
            target_name = s(chosen_row.get("Zendesk Target Field"))
            target_type = normalize_label(chosen_row.get("Zendesk Target Type"))

            if "drop-down" in target_type or "drop down" in target_type or "multiselect" in target_type:
                mapped_value, mapped_reason = self.map_custom_value(sf_api, chosen_value)
                if mapped_value is None:
                    if sf_api in {"BP_Query_Related_Issue__c", "Query_Related_Issue_B2B__c"}:
                        v = normalize_label(chosen_value)
                        for vmrow in self.value_rows:
                            if s(vmrow.get("Salesforce API Name")) == sf_api and normalize_label(vmrow.get("Salesforce Value")) == v:
                                mapped_value = s(vmrow.get("Zendesk Tag")) or s(vmrow.get("Zendesk Value"))
                                mapped_reason = "leaf_mapping"
                                break
                    if mapped_value is None or s(mapped_value) == "":
                        report.append({
                            "case_id": s(case.get("Id")),
                            "field": sf_api,
                            "target_field_id": field_id,
                            "status": "skipped",
                            "reason": mapped_reason or "no_value_mapping",
                            "source_value": s(chosen_value),
                        })
                        continue
                value = mapped_value
                if value is None or s(value) == "":
                    report.append({
                        "case_id": s(case.get("Id")),
                        "field": sf_api,
                        "target_field_id": field_id,
                        "status": "skipped",
                        "reason": "empty_mapped_value",
                        "source_value": s(chosen_value),
                    })
                    continue
            elif "check-box" in target_type or "checkbox" in target_type:
                value = parse_checkbox(chosen_value)
                if value is None:
                    continue
            elif "integer" in target_type:
                raw_numeric = s(chosen_value)
                if not re.fullmatch(r"[-+]?\d+", raw_numeric):
                    report.append({
                        "case_id": s(case.get("Id")),
                        "field": sf_api,
                        "target_field_id": field_id,
                        "status": "skipped",
                        "reason": "invalid_integer_value",
                        "source_value": raw_numeric,
                    })
                    continue
                value = int(raw_numeric)
            elif "numeric" in target_type:
                value = parse_numeric(chosen_value)
                if value is None:
                    continue
            else:
                value = s(chosen_value)

            result.append({"id": int(field_id), "value": value})
            used_ids.add(field_id)

        # Deduplicate by target ID
        dedup: Dict[int, Dict[str, Any]] = {}
        for item in result:
            dedup[int(item["id"])] = item
        return list(dedup.values())
