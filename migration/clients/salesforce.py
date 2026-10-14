"""Salesforce API Client with OAuth, automatic pagination, and resilient retries."""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Set

import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def quote_soql(value: str) -> str:
    """Escape single quotes for SOQL literals."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def soql_ids(ids: Iterable[str]) -> str:
    """Format an iterable of IDs for SOQL 'IN (...)' clauses."""
    cleaned = [quote_soql(str(x).strip()) for x in ids if str(x).strip()]
    return ", ".join(f"'{x}'" for x in cleaned)


def chunks(values: List[str], size: int) -> Iterable[List[str]]:
    """Yield successive slices of values of size."""
    for i in range(0, len(values), size):
        yield values[i : i + size]


class SalesforceClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, api_version: str = "v59.0"):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_version = api_version
        self.access_token: str = ""
        self.instance_url: str = self.base_url
        self.session = requests.Session()
        self._describe_cache: Dict[str, Set[str]] = {}

    def authenticate(self) -> None:
        """Authenticate with Salesforce OAuth2 using client_credentials grant."""
        token_url = f"{self.base_url}/services/oauth2/token"
        response = self.session.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=60,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Salesforce OAuth authentication failed (HTTP {response.status_code}):\n{response.text[:3000]}"
            )
        data = response.json()
        self.access_token = data["access_token"]
        self.instance_url = data.get("instance_url", self.base_url).rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """Make an authenticated HTTP request with retry logic and rate limit monitoring."""
        url = path if path.startswith("http") else f"{self.instance_url}{path}"

        for attempt in range(1, 7):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=90,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 6:
                    raise
                time.sleep(min(60, 2 ** (attempt - 1)))
                continue

            if response.status_code == 401 and attempt == 1:
                self.authenticate()
                continue

            api_usage = response.headers.get("Sforce-Limit-Info", "")
            if api_usage and "api-usage=" in api_usage:
                try:
                    usage = api_usage.split("api-usage=", 1)[1].split(",", 1)[0]
                    used, maximum = usage.split("/", 1)
                    if int(maximum) and int(used) / int(maximum) >= 0.90:
                        print(f"WARNING: Salesforce API daily usage is high: {used}/{maximum}")
                except (ValueError, IndexError):
                    pass

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 6:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(60, 2 ** (attempt - 1))
                except ValueError:
                    delay = min(60, 2 ** (attempt - 1))
                time.sleep(delay)
                continue

            return response

        raise RuntimeError(f"Salesforce request to {url} failed after 6 retries.")

    def describe_fields(self, object_name: str) -> Set[str]:
        """Describe an SObject and return the set of available field API names."""
        if object_name in self._describe_cache:
            return self._describe_cache[object_name]

        response = self.request(
            "GET",
            f"/services/data/{self.api_version}/sobjects/{object_name}/describe",
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Salesforce describe failed for {object_name} (HTTP {response.status_code}):\n{response.text[:3000]}"
            )
        fields = {str(f.get("name")) for f in response.json().get("fields", []) if f.get("name")}
        self._describe_cache[object_name] = fields
        return fields

    def query(self, soql: str, progress_label: Optional[str] = None) -> List[Dict[str, Any]]:
        """Run a SOQL query with full pagination."""
        response = self.request(
            "GET",
            f"/services/data/{self.api_version}/query",
            params={"q": soql},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Salesforce SOQL query failed (HTTP {response.status_code}):\n{response.text[:4000]}"
            )

        data = response.json()
        records: List[Dict[str, Any]] = list(data.get("records", []))
        total = data.get("totalSize", "?")
        if progress_label:
            print(f"[Salesforce] {progress_label}: {len(records):,}/{total} records loaded", flush=True)

        while not data.get("done", True):
            next_url = data.get("nextRecordsUrl")
            if not next_url:
                break
            response = self.request("GET", next_url)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Salesforce pagination failed (HTTP {response.status_code}):\n{response.text[:4000]}"
                )
            data = response.json()
            records.extend(data.get("records", []))
            if progress_label:
                print(f"[Salesforce] {progress_label}: {len(records):,}/{total} records loaded", flush=True)

        return records

    def query_by_id_chunks(
        self,
        query_template: str,
        ids: List[str],
        batch_size: int = 200,
        label: str = "Query",
    ) -> List[Dict[str, Any]]:
        """Run SOQL queries chunked by an 'IN ({id_list})' parameter."""
        all_records: List[Dict[str, Any]] = []
        unique_ids = sorted({str(i).strip() for i in ids if str(i).strip()})
        if not unique_ids:
            return []

        total_batches = (len(unique_ids) + batch_size - 1) // batch_size
        for batch_num, id_chunk in enumerate(chunks(unique_ids, batch_size), start=1):
            soql = query_template.format(ids=soql_ids(id_chunk))
            batch_records = self.query(soql)
            all_records.extend(batch_records)
            if total_batches > 1 and (batch_num % 10 == 0 or batch_num == total_batches):
                print(f"  [Salesforce] {label}: batch {batch_num}/{total_batches} complete ({len(all_records):,} records)", flush=True)

        return all_records
