"""Migration business logic engine for field mapping, identity resolution, and ticket preparation."""
from migration.engine.field_mapper import DatabaseFieldMapper
from migration.engine.comment_builder import build_ticket_comments
from migration.engine.identity_resolver import IdentityResolver
from migration.engine.payload_builder import build_ticket_payload

__all__ = [
    "DatabaseFieldMapper",
    "build_ticket_comments",
    "IdentityResolver",
    "build_ticket_payload",
]
