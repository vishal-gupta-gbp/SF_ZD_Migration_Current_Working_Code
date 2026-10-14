"""SQLAlchemy models for database-backed migration state and data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    Text,
)

from migration.db.connection import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MigrationRun(Base):
    __tablename__ = "migration_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    start_date = Column(String(50), nullable=True)
    end_date = Column(String(50), nullable=True)
    max_tickets = Column(Integer, default=0)
    status = Column(String(50), default="in_progress")  # in_progress, completed, failed
    started_at = Column(DateTime, default=utc_now)
    completed_at = Column(DateTime, nullable=True)
    summary_json = Column(JSON, nullable=True)


class MappingField(Base):
    __tablename__ = "mapping_fields"

    id = Column(Integer, primary_key=True, autoincrement=True)
    salesforce_api_name = Column(String(255), index=True, nullable=False)
    salesforce_label = Column(Text, nullable=True)
    salesforce_type = Column(String(100), nullable=True)
    zendesk_target_field = Column(Text, nullable=True)
    zendesk_target_field_id = Column(BigInteger, index=True, nullable=True)
    zendesk_target_type = Column(String(100), nullable=True)
    mapping_status = Column(String(100), nullable=True)
    transformation_notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_mapping_fields_api_id", "salesforce_api_name", "zendesk_target_field_id"),
    )


class MappingValue(Base):
    __tablename__ = "mapping_values"

    id = Column(Integer, primary_key=True, autoincrement=True)
    salesforce_api_name = Column(String(255), index=True, nullable=False)
    salesforce_value = Column(Text, nullable=True)
    zendesk_value = Column(Text, nullable=True)
    zendesk_tag = Column(Text, nullable=True)
    match_status = Column(String(100), nullable=True)


class SFCase(Base):
    __tablename__ = "sf_cases"

    id = Column(String(50), primary_key=True)  # Salesforce Case ID
    case_number = Column(String(50), index=True, nullable=True)
    account_id = Column(String(50), index=True, nullable=True)
    contact_id = Column(String(50), index=True, nullable=True)
    owner_id = Column(String(50), index=True, nullable=True)
    created_date = Column(DateTime, index=True, nullable=True)
    subject = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    status = Column(Text, nullable=True)
    priority = Column(Text, nullable=True)
    origin = Column(Text, nullable=True)
    type = Column(Text, nullable=True)
    record_type_id = Column(String(50), index=True, nullable=True)
    is_phone_case = Column(Boolean, default=False, index=True)
    custom_fields_json = Column(JSON, nullable=True)
    raw_data_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utc_now)


class SFContact(Base):
    __tablename__ = "sf_contacts"

    id = Column(String(50), primary_key=True)
    account_id = Column(String(50), index=True, nullable=True)
    name = Column(Text, nullable=True)
    first_name = Column(Text, nullable=True)
    last_name = Column(Text, nullable=True)
    email = Column(Text, nullable=True)
    phone = Column(Text, nullable=True)
    mobile_phone = Column(Text, nullable=True)
    raw_data_json = Column(JSON, nullable=True)


class SFAccount(Base):
    __tablename__ = "sf_accounts"

    id = Column(String(50), primary_key=True)
    name = Column(Text, nullable=True)
    type = Column(Text, nullable=True)
    raw_data_json = Column(JSON, nullable=True)


class SFUser(Base):
    __tablename__ = "sf_users"

    id = Column(String(50), primary_key=True)
    name = Column(Text, nullable=True)
    email = Column(Text, nullable=True)
    username = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    user_type = Column(Text, nullable=True)


class SFRecordType(Base):
    __tablename__ = "sf_record_types"

    id = Column(String(50), primary_key=True)
    name = Column(Text, nullable=True)
    developer_name = Column(Text, nullable=True)
    sobject_type = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)


class SFEmailMessage(Base):
    __tablename__ = "sf_email_messages"

    id = Column(String(50), primary_key=True)
    parent_id = Column(String(50), index=True, nullable=False)  # Case ID
    created_by_id = Column(String(50), index=True, nullable=True)
    created_date = Column(DateTime, index=True, nullable=True)
    message_date = Column(DateTime, nullable=True)
    incoming = Column(Boolean, default=False)
    from_address = Column(Text, nullable=True)
    to_address = Column(Text, nullable=True)
    cc_address = Column(Text, nullable=True)
    bcc_address = Column(Text, nullable=True)
    from_name = Column(Text, nullable=True)
    subject = Column(Text, nullable=True)
    text_body = Column(Text, nullable=True)
    html_body = Column(Text, nullable=True)
    status = Column(Text, nullable=True)
    is_private_draft = Column(Boolean, default=False)
    has_attachment = Column(Boolean, default=False)
    raw_data_json = Column(JSON, nullable=True)


class SFS3Record(Base):
    __tablename__ = "sf_s3_records"

    id = Column(String(50), primary_key=True)
    name = Column(Text, nullable=True)
    bp_case_id = Column(String(50), index=True, nullable=False)  # Case ID
    bp_record_id = Column(String(50), index=True, nullable=True)  # EmailMessage ID or Case ID
    bp_s3_url = Column(Text, nullable=True)
    bp_fallback_url = Column(Text, nullable=True)
    created_date = Column(DateTime, nullable=True)


class SFTask(Base):
    __tablename__ = "sf_tasks"

    id = Column(String(50), primary_key=True)
    what_id = Column(String(50), index=True, nullable=False)  # Case ID
    who_id = Column(String(50), nullable=True)
    subject = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    created_date = Column(DateTime, nullable=True)
    recording_urls_json = Column(JSON, nullable=True)


class ZendeskOrganization(Base):
    __tablename__ = "zendesk_organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    salesforce_account_id = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(Text, nullable=True)
    external_id = Column(String(255), unique=True, index=True, nullable=False)
    zendesk_org_id = Column(BigInteger, index=True, nullable=True)
    status = Column(String(50), default="completed")  # existing, completed, failed
    error_message = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class ZendeskUser(Base):
    __tablename__ = "zendesk_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    identity_key = Column(String(500), unique=True, index=True, nullable=False)
    salesforce_contact_id = Column(String(50), index=True, nullable=True)
    source_email = Column(Text, nullable=True)
    zendesk_email = Column(Text, nullable=True)
    phone = Column(Text, nullable=True)
    salesforce_account_id = Column(String(50), index=True, nullable=True)
    zendesk_org_id = Column(BigInteger, nullable=True)
    external_id = Column(String(255), index=True, nullable=True)
    zendesk_user_id = Column(BigInteger, index=True, nullable=True)
    is_synthetic = Column(Boolean, default=False)
    status = Column(String(50), default="completed")  # existing, completed, failed
    error_message = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class MigrationAttachment(Base):
    __tablename__ = "migration_attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String(50), index=True, nullable=False)
    comment_id = Column(String(50), index=True, nullable=True)  # EmailMessage ID
    s3_record_id = Column(String(50), index=True, nullable=True)
    file_name = Column(Text, nullable=True)
    original_s3_url = Column(Text, nullable=True)
    migration_s3_url = Column(Text, nullable=True)
    local_file_path = Column(Text, nullable=True)
    bucket = Column(Text, nullable=True)
    key = Column(Text, nullable=True)
    file_size_bytes = Column(BigInteger, nullable=True)
    content_type = Column(Text, nullable=True)
    sha256 = Column(String(64), index=True, nullable=True)
    download_status = Column(String(50), default="pending", index=True)  # success, failed, pending
    download_error = Column(Text, nullable=True)
    upload_status = Column(String(50), default="pending", index=True)  # success, failed, pending
    upload_token = Column(Text, nullable=True)
    upload_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class MigrationTicket(Base):
    __tablename__ = "migration_tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String(50), unique=True, index=True, nullable=False)
    case_number = Column(String(50), index=True, nullable=True)
    ticket_type = Column(String(50), index=True, default="standard")  # standard, phone
    status = Column(String(50), index=True, default="prepared")
    # Statuses: prepared, importing, completed, completed_with_warnings, existing, failed, manual_review_required
    prepared_payload_json = Column(JSON, nullable=True)
    audit_json = Column(JSON, nullable=True)
    zendesk_ticket_id = Column(BigInteger, index=True, nullable=True)
    zendesk_external_id = Column(String(255), index=True, nullable=True)
    attempts = Column(Integer, default=0)
    http_status = Column(String(50), nullable=True)
    error_type = Column(Text, nullable=True)
    failure_reason = Column(Text, nullable=True)
    error_detail = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class MigrationAuditLog(Base):
    __tablename__ = "migration_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, index=True, nullable=True)
    case_id = Column(String(50), index=True, nullable=True)
    stage = Column(String(50), index=True, nullable=False)
    level = Column(String(20), default="INFO")  # INFO, WARNING, ERROR
    status = Column(String(50), nullable=True)
    message = Column(Text, nullable=True)
    details_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utc_now)
