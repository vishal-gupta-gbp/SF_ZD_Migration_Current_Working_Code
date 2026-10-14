"""Configuration management driven by environment variables."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    dotenv_path = BASE_DIR / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
    else:
        load_dotenv()
except ImportError:
    pass


@dataclass
class SalesforceConfig:
    base_url: str = ""
    consumer_key: str = ""
    consumer_secret: str = ""
    api_version: str = "v59.0"


@dataclass
class ZendeskConfig:
    subdomain: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""
    base_url: str = ""

    def __post_init__(self):
        if self.subdomain and not self.base_url:
            self.base_url = f"https://{self.subdomain}.zendesk.com"


@dataclass
class AWSConfig:
    access_key_id: str = ""
    secret_access_key: str = ""
    region: str = "us-east-1"


@dataclass
class MigrationSettings:
    database_url: str = "postgresql+psycopg2://migration_user:migration_pass@localhost:5432/migration_db"
    salesforce: SalesforceConfig = field(default_factory=SalesforceConfig)
    zendesk: ZendeskConfig = field(default_factory=ZendeskConfig)
    aws: AWSConfig = field(default_factory=AWSConfig)
    s3_base_url: str = ""
    mapping_file: Path = field(default_factory=lambda: BASE_DIR / "Salesforce_to_Zendesk_Case_Field_Mapping.xlsx")
    workspace_dir: Path = field(default_factory=lambda: BASE_DIR / "migration_workspace")
    max_tickets: int = 0
    archive_immediately: bool = False
    agent_comment_email: str = "akarsh.shrivastava@groupbayport.com"
    synthetic_user_domain: str = "sfmigrateduser.com"
    export_batch_size: int = 200
    s3_download_concurrency: int = 8
    zd_upload_concurrency: int = 5
    zd_bulk_import_batch_size: int = 100

    # Constant target fields
    association_type_field_id: int = 30064365242012
    group_custom_field_id: int = 30114110837404


def load_config(config_path: Optional[Path | str] = None) -> MigrationSettings:
    """Load configuration from environment variables (with optional JSON file overrides)."""
    cfg_data: Dict[str, Any] = {}

    if config_path:
        p = Path(config_path)
        target_config = p if p.is_absolute() else BASE_DIR / p
        if target_config.exists():
            try:
                with open(target_config, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
            except Exception as exc:
                print(f"Warning: could not read {target_config}: {exc}")

    sf_raw = cfg_data.get("salesforce", {})
    sf_cfg = SalesforceConfig(
        base_url=os.getenv("SF_BASE_URL", sf_raw.get("base_url", "https://bayport.my.salesforce.com")),
        consumer_key=os.getenv("SF_CONSUMER_KEY", sf_raw.get("consumer_key", "")),
        consumer_secret=os.getenv("SF_CONSUMER_SECRET", sf_raw.get("consumer_secret", "")),
        api_version=os.getenv("SF_API_VERSION", sf_raw.get("api_version", "v59.0")),
    )

    zd_raw = cfg_data.get("zendesk", {})
    subdomain = os.getenv("ZD_SUBDOMAIN", zd_raw.get("subdomain", "grpbayport"))
    raw_scope = os.getenv("ZD_SCOPE", zd_raw.get("scope", ""))
    scope = raw_scope.strip('"\'') if raw_scope else ""
    zd_cfg = ZendeskConfig(
        subdomain=subdomain,
        client_id=os.getenv("ZD_CLIENT_ID", zd_raw.get("client_id", "")),
        client_secret=os.getenv("ZD_CLIENT_SECRET", zd_raw.get("client_secret", "")),
        scope=scope,
    )

    aws_raw = cfg_data.get("aws", {})
    aws_cfg = AWSConfig(
        access_key_id=os.getenv("AWS_ACCESS_KEY_ID", aws_raw.get("access_key_id", "")),
        secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", aws_raw.get("secret_access_key", "")),
        region=os.getenv("AWS_DEFAULT_REGION", os.getenv("AWS_REGION", aws_raw.get("region", "us-east-1"))),
    )

    s3_raw = cfg_data.get("s3", {})
    s3_base = os.getenv("S3_BASE_URL", s3_raw.get("base_url", ""))

    mapping_str = os.getenv("MAPPING_FILE", cfg_data.get("mapping_file", "Salesforce_to_Zendesk_Case_Field_Mapping.xlsx"))
    mapping_p = Path(mapping_str)
    mapping_file = mapping_p if mapping_p.is_absolute() else BASE_DIR / mapping_p

    workspace_str = os.getenv("WORKSPACE_DIR", cfg_data.get("workspace_dir", "migration_workspace"))
    workspace_p = Path(workspace_str)
    workspace_dir = workspace_p if workspace_p.is_absolute() else BASE_DIR / workspace_p

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        # Default to postgres or sqlite based on environment
        if os.getenv("POSTGRES_HOST") or os.getenv("DOCKER_CONTAINER"):
            db_url = "postgresql+psycopg2://migration_user:migration_pass@postgres:5432/migration_db"
        else:
            db_url = f"sqlite:///{workspace_dir}/migration.db"

    return MigrationSettings(
        database_url=db_url,
        salesforce=sf_cfg,
        zendesk=zd_cfg,
        aws=aws_cfg,
        s3_base_url=s3_base,
        mapping_file=mapping_file,
        workspace_dir=workspace_dir,
        max_tickets=int(os.getenv("MAX_TICKETS", cfg_data.get("max_tickets", 0))),
        archive_immediately=os.getenv("ARCHIVE_IMMEDIATELY", "").lower() in {"true", "1", "yes"},
        agent_comment_email=os.getenv("AGENT_COMMENT_EMAIL", "akarsh.shrivastava@groupbayport.com"),
        synthetic_user_domain=os.getenv("SYNTHETIC_USER_DOMAIN", "sfmigrateduser.com"),
        export_batch_size=int(os.getenv("EXPORT_BATCH_SIZE", 200)),
        s3_download_concurrency=int(os.getenv("S3_DOWNLOAD_CONCURRENCY", 8)),
        zd_upload_concurrency=int(os.getenv("ZD_UPLOAD_CONCURRENCY", 5)),
        zd_bulk_import_batch_size=int(os.getenv("ZD_BULK_IMPORT_BATCH_SIZE", 100)),
    )
