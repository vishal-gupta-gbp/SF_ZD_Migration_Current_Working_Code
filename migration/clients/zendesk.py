"""Zendesk REST and Bulk Import API Client with rate-limiting and job polling."""
from __future__ import annotations

import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
ZENDESK_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024


class ZendeskBulkImportError(RuntimeError):
    def __init__(self, status_code: int, response_text: str):
        self.status_code = int(status_code)
        super().__init__(f"Zendesk bulk ticket import failed (HTTP {status_code}):\n{response_text}")


class ZendeskClient:
    def __init__(self, subdomain: str, client_id: str, client_secret: str, scope: str):
        self.subdomain = subdomain
        self.base_url = f"https://{subdomain}.zendesk.com"
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.access_token: str = ""
        self.session = requests.Session()
        self._groups_cache: Optional[List[Dict[str, Any]]] = None
        self._brands_cache: Optional[List[Dict[str, Any]]] = None

    def authenticate(self) -> None:
        """Authenticate using client credentials grant with 2 retries."""
        token_url = f"{self.base_url}/oauth/tokens"
        last_error: Any = None
        for attempt in range(1, 4):
            try:
                response = self.session.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": self.scope,
                    },
                    timeout=60,
                )
                if response.status_code == 200:
                    self.access_token = response.json()["access_token"]
                    return
                last_error = RuntimeError(
                    f"Zendesk OAuth authentication failed (HTTP {response.status_code}):\n{response.text[:3000]}"
                )
                if attempt < 3:
                    print(f"  [Zendesk] Auth attempt {attempt} failed (HTTP {response.status_code}). Retrying ({attempt}/2)...", flush=True)
                    time.sleep(2)
                    continue
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    print(f"  [Zendesk] Auth attempt {attempt} exception ({exc}). Retrying ({attempt}/2)...", flush=True)
                    time.sleep(2)
                    continue
                raise
        if isinstance(last_error, Exception):
            raise last_error
        raise RuntimeError(f"Zendesk OAuth authentication failed: {last_error}")

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """Execute request with rate limit handling, automatic retry, and token refresh."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"

        for attempt in range(1, 7):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    timeout=90,
                )
            except (requests.Timeout, requests.ConnectionError, requests.RequestException) as exc:
                if attempt >= 3 and attempt == 6:
                    raise
                print(f"  [Zendesk] Request to {url} failed ({exc}). Retrying attempt {attempt}/2...", flush=True)
                time.sleep(min(60, 2 ** (attempt - 1)))
                continue

            if response.status_code == 401 and attempt == 1:
                self.authenticate()
                continue

            # Respect Zendesk rate limit headers
            try:
                remaining = int(response.headers.get("ratelimit-remaining", "999999"))
                reset = int(response.headers.get("ratelimit-reset", "0"))
                if remaining <= 5 and reset > 0 and response.status_code not in RETRYABLE_STATUS_CODES:
                    time.sleep(reset + 1)
            except (TypeError, ValueError):
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

        raise RuntimeError(f"Zendesk request to {url} failed after 6 retries.")

    def list_all(self, path: str, collection_key: str, label: str) -> List[Dict[str, Any]]:
        """Paginate through a Zendesk list endpoint."""
        records: List[Dict[str, Any]] = []
        url = path
        params: Optional[Dict[str, Any]] = {"page[size]": 100}
        pages = 0

        while url:
            resp = self.request("GET", url, params=params)
            if resp.status_code != 200:
                # Fall back to per_page style if cursor not supported
                if pages == 0 and params and "page[size]" in params:
                    params = {"per_page": 100}
                    resp = self.request("GET", path, params=params)
                if resp.status_code != 200:
                    raise RuntimeError(f"Zendesk {label} listing failed (HTTP {resp.status_code}): {resp.text[:1000]}")

            data = resp.json()
            records.extend(data.get(collection_key, []))
            pages += 1
            if pages % 25 == 0:
                print(f"  [Zendesk] {label}: loaded {len(records):,} records ({pages} pages)...", flush=True)

            links = data.get("links") or {}
            next_url = links.get("next") or data.get("next_page")
            if next_url:
                url = next_url
                params = None
            else:
                url = ""

        return records

    def show_many_by_external_ids(
        self, endpoint: str, collection_key: str, external_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch records by chunks of 100 external IDs."""
        result: Dict[str, Dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(str(x).strip() for x in external_ids if str(x).strip()))

        for i in range(0, len(unique_ids), 100):
            batch = unique_ids[i : i + 100]
            resp = self.request("GET", endpoint, params={"external_ids": ",".join(batch)})
            if resp.status_code == 200:
                for item in resp.json().get(collection_key, []):
                    ext = str(item.get("external_id") or "").casefold()
                    if ext:
                        result[ext] = item

        return result

    def wait_for_job(self, job_dict: Dict[str, Any], label: str, timeout_seconds: int = 1800) -> Dict[str, Any]:
        """Poll Zendesk job status until completion or failure."""
        status_info = job_dict.get("job_status") or job_dict
        job_id = str(status_info.get("id") or "")
        job_url = str(status_info.get("url") or "")
        if not job_id and not job_url:
            raise RuntimeError(f"Zendesk did not return a job ID or URL for {label}: {job_dict}")

        poll_url = job_url or f"/api/v2/job_statuses/{job_id}.json"
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            resp = self.request("GET", poll_url)
            if resp.status_code != 200:
                raise RuntimeError(f"Zendesk {label} job polling failed (HTTP {resp.status_code}): {resp.text[:1000]}")
            data = resp.json()
            state = data.get("job_status") or data
            status = str(state.get("status") or "").lower()

            if status == "completed":
                return state
            if status in {"failed", "killed"}:
                raise RuntimeError(f"Zendesk {label} job {job_id} failed: {state.get('message', '')}")

            time.sleep(2)

        raise TimeoutError(f"Timed out waiting for Zendesk {label} job {job_id} after {timeout_seconds} seconds.")

    def bulk_create(self, endpoint: str, collection_key: str, payloads: List[Dict[str, Any]], label: str) -> bool:
        """Submit batches of <=100 to a bulk creation endpoint."""
        if not payloads:
            return True

        for i in range(0, len(payloads), 100):
            batch = payloads[i : i + 100]
            resp = self.request("POST", endpoint, json_body={collection_key: batch})
            if resp.status_code == 403:
                print(f"  [Zendesk] Bulk {label} is not enabled (HTTP 403). Falling back to single-record calls.", flush=True)
                return False
            if resp.status_code not in {200, 201, 202}:
                print(f"  [Zendesk] Bulk {label} batch rejected (HTTP {resp.status_code}); falling back for batch.", flush=True)
                return False
            try:
                self.wait_for_job(resp.json(), label)
            except Exception as exc:
                print(f"  [Zendesk] Warning: {label} job warning: {exc}", flush=True)

        return True

    def find_user_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        resp = self.request("GET", "/api/v2/users.json", params={"external_id": external_id, "per_page": 100})
        if resp.status_code == 200:
            users = resp.json().get("users", [])
            return users[0] if users else None
        return None

    def find_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        clean_email = str(email).strip().lower()
        resp = self.request("GET", "/api/v2/users/search.json", params={"query": clean_email, "per_page": 100})
        if resp.status_code == 200:
            users = resp.json().get("users", [])
            for u in users:
                if str(u.get("email") or "").strip().lower() == clean_email:
                    return u
            return users[0] if users else None
        return None

    def find_group_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        target = str(name).strip().lower()
        if not target:
            return None
        if self._groups_cache is None:
            self._groups_cache = self.list_all("/api/v2/groups.json", "groups", "groups")
        for g in self._groups_cache:
            if str(g.get("name") or "").strip().lower() == target:
                return g
        return None

    def find_brand_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        target = str(name).strip().lower()
        if not target:
            return None
        if self._brands_cache is None:
            self._brands_cache = self.list_all("/api/v2/brands.json", "brands", "brands")
        for b in self._brands_cache:
            if str(b.get("name") or "").strip().lower() == target:
                return b
        return None

    def create_or_update_organization(self, name: str, external_id: str) -> Dict[str, Any]:
        payload = {"organization": {"name": name or f"Salesforce {external_id}", "external_id": external_id}}
        resp = self.request("POST", "/api/v2/organizations/create_or_update.json", json_body=payload)
        if resp.status_code not in {200, 201}:
            raise RuntimeError(f"Zendesk organization create/update failed (HTTP {resp.status_code}): {resp.text[:1000]}")
        return resp.json().get("organization", {})

    def create_end_user(
        self,
        name: str,
        email: str,
        external_id: str,
        organization_id: Optional[int] = None,
        phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "user": {
                "name": name or email,
                "email": email,
                "external_id": external_id,
                "role": "end-user",
                "skip_verify_email": True,
            }
        }
        if organization_id:
            body["user"]["organization_id"] = organization_id
        if phone:
            body["user"]["phone"] = phone

        resp = self.request("POST", "/api/v2/users.json", json_body=body)
        if resp.status_code not in {200, 201}:
            raise RuntimeError(f"Zendesk end user creation failed (HTTP {resp.status_code}): {resp.text[:1000]}")
        return resp.json().get("user", {})

    def upload_file(self, local_path: Path, filename: str, content_type: Optional[str] = None) -> str:
        """Upload a local file and return the single-use upload token."""
        if not local_path.exists():
            raise FileNotFoundError(f"Local attachment file not found: {local_path}")
        size = local_path.stat().st_size
        if size > ZENDESK_ATTACHMENT_MAX_BYTES:
            raise RuntimeError(f"Attachment exceeds 50 MB limit: {filename} ({size:,} bytes)")

        mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        url = f"{self.base_url}/api/v2/uploads.json"
        params = {"filename": filename}

        for attempt in range(1, 7):
            try:
                with local_path.open("rb") as f:
                    resp = self.session.post(
                        url,
                        params=params,
                        data=f,
                        headers={
                            "Authorization": f"Bearer {self.access_token}",
                            "Content-Type": mime,
                            "Accept": "application/json",
                        },
                        timeout=180,
                    )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 6:
                    raise
                time.sleep(min(60, 2 ** (attempt - 1)))
                continue

            if resp.status_code == 401 and attempt == 1:
                self.authenticate()
                continue

            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < 6:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(60, 2 ** (attempt - 1))
                except ValueError:
                    delay = min(60, 2 ** (attempt - 1))
                time.sleep(delay)
                continue

            if resp.status_code != 201:
                if attempt < 3:
                    print(f"  [Zendesk] Upload failed for {filename} (HTTP {resp.status_code}). Retrying attempt {attempt}/2...", flush=True)
                    time.sleep(2)
                    continue
                raise RuntimeError(f"Zendesk upload failed for {filename} (HTTP {resp.status_code}):\n{resp.text[:2000]}")

            token = resp.json().get("upload", {}).get("token")
            if not token:
                raise RuntimeError(f"Zendesk upload returned no token for {filename}")
            return str(token)

        raise RuntimeError(f"Zendesk upload failed after retries: {filename}")

    def submit_bulk_ticket_import_job(
        self, batch_payloads: List[Dict[str, Any]], archive_immediately: bool = False
    ) -> Dict[str, Any]:
        """Submit up to 100 tickets to the Bulk Import API with 2-time retry mechanism."""
        last_error: Any = None
        for attempt in range(1, 4):
            try:
                resp = self.request(
                    "POST",
                    "/api/v2/imports/tickets/create_many.json",
                    params={"archive_immediately": str(archive_immediately).lower()},
                    json_body={"tickets": batch_payloads},
                )
                if resp.status_code in {200, 201, 202}:
                    data = resp.json()
                    job = data.get("job_status") or data
                    return job
                last_error = ZendeskBulkImportError(resp.status_code, resp.text[:5000])
                if attempt < 3:
                    print(f"  [Zendesk] Bulk import job submission attempt {attempt} failed (HTTP {resp.status_code}). Retrying ({attempt}/2)...", flush=True)
                    time.sleep(2)
                    continue
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    print(f"  [Zendesk] Bulk import job submission attempt {attempt} exception ({exc}). Retrying ({attempt}/2)...", flush=True)
                    time.sleep(2)
                    continue
                raise
        if isinstance(last_error, Exception):
            raise last_error
        raise ZendeskBulkImportError(500, f"Bulk ticket import failed after 2 retries: {last_error}")

    def import_single_ticket(
        self, ticket_payload: Dict[str, Any], archive_immediately: bool = False
    ) -> Dict[str, Any]:
        """Single ticket import fallback with 2-time retry mechanism."""
        last_error: Any = None
        for attempt in range(1, 4):
            try:
                resp = self.request(
                    "POST",
                    "/api/v2/imports/tickets.json",
                    params={"archive_immediately": str(archive_immediately).lower()},
                    json_body={"ticket": ticket_payload},
                )
                if resp.status_code == 201:
                    return resp.json().get("ticket", {})
                last_error = RuntimeError(f"Single ticket import failed (HTTP {resp.status_code}): {resp.text[:2000]}")
                if attempt < 3:
                    print(f"  [Zendesk] Single ticket import attempt {attempt} failed (HTTP {resp.status_code}). Retrying ({attempt}/2)...", flush=True)
                    time.sleep(1.5)
                    continue
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    print(f"  [Zendesk] Single ticket import attempt {attempt} exception ({exc}). Retrying ({attempt}/2)...", flush=True)
                    time.sleep(1.5)
                    continue
                raise
        if isinstance(last_error, Exception):
            raise last_error
        raise RuntimeError(str(last_error))

    def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
        resp = self.request("GET", f"/api/v2/tickets/{int(ticket_id)}.json")
        if resp.status_code != 200:
            raise RuntimeError(f"Zendesk get ticket {ticket_id} failed (HTTP {resp.status_code})")
        return resp.json().get("ticket", {})

    def find_ticket_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        query = f"type:ticket external_id:{external_id}"
        resp = self.request("GET", "/api/v2/search.json", params={"query": query, "per_page": 100})
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        tickets = [r for r in results if r.get("result_type") == "ticket" or "ticket" in str(r.get("url") or "").lower()]
        return tickets[0] if tickets else None

    def _index_ticket_identifiers(self, ticket_item: Dict[str, Any], index_dict: Dict[str, Dict[str, Any]]) -> None:
        """Helper to index a ticket by external_id, SF Ticket Number custom field, and subject case number."""
        ext = str(ticket_item.get("external_id") or "").strip()
        if ext:
            index_dict[ext] = ticket_item
            index_dict[ext.lower()] = ticket_item

        # Index by custom_fields (SF Ticket Number custom field ID: 30867591650204)
        for cf in ticket_item.get("custom_fields", []):
            if cf.get("id") == 30867591650204 and cf.get("value"):
                cnum = str(cf["value"]).strip()
                if cnum:
                    index_dict[cnum] = ticket_item
                    index_dict[cnum.lower()] = ticket_item

        # Index by CaseNumber from Subject (e.g. "Salesforce Case 02879137")
        subj = str(ticket_item.get("subject") or "")
        match = re.search(r"\b(\d{7,10})\b", subj)
        if match:
            cnum = match.group(1)
            index_dict[cnum] = ticket_item
            index_dict[cnum.lower()] = ticket_item

    def batch_find_tickets_by_external_ids(
        self,
        external_ids: List[str],
        case_numbers: Optional[List[str]] = None,
        chunk_size: int = 30,
    ) -> Dict[str, Dict[str, Any]]:
        """Find multiple tickets by external_ids and case_numbers in Zendesk using chunked search queries."""
        result: Dict[str, Dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(str(x).strip() for x in external_ids if str(x).strip()))
        if unique_ids:
            for i in range(0, len(unique_ids), chunk_size):
                chunk = unique_ids[i : i + chunk_size]
                query = "type:ticket " + " ".join(f"external_id:{cid}" for cid in chunk)
                resp = self.request("GET", "/api/v2/search.json", params={"query": query, "per_page": 100})
                if resp.status_code == 200:
                    for item in resp.json().get("results", []):
                        self._index_ticket_identifiers(item, result)

        if case_numbers:
            # Query any case numbers that were not resolved by external_id
            unresolved_nums = [
                str(cn).strip() for cn in case_numbers
                if str(cn).strip() and str(cn).strip() not in result and str(cn).strip().lower() not in result
            ]
            unique_nums = list(dict.fromkeys(unresolved_nums))
            for i in range(0, len(unique_nums), chunk_size):
                chunk = unique_nums[i : i + chunk_size]
                query = "type:ticket " + " ".join(f'"{cnum}"' for cnum in chunk)
                resp = self.request("GET", "/api/v2/search.json", params={"query": query, "per_page": 100})
                if resp.status_code == 200:
                    for item in resp.json().get("results", []):
                        self._index_ticket_identifiers(item, result)

        return result

    def find_tickets_in_date_range(
        self, start_date: str, end_date: str, max_pages: int = 50
    ) -> Dict[str, Dict[str, Any]]:
        """Paginate Zendesk search for all tickets created in date range and map identifiers -> ticket."""
        result: Dict[str, Dict[str, Any]] = {}
        query = f"type:ticket created>={start_date}T00:00:00Z created<={end_date}T23:59:59Z"
        url = "/api/v2/search.json"
        params: Optional[Dict[str, Any]] = {"query": query, "per_page": 100}
        pages = 0

        while url and pages < max_pages:
            resp = self.request("GET", url, params=params)
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get("results", [])
            for item in items:
                self._index_ticket_identifiers(item, result)

            pages += 1
            next_url = data.get("next_page")
            if next_url and len(items) == 100:
                url = next_url
                params = None
            else:
                break

        return result

    def validate_ticket_external_id(
        self, ticket_id: int, expected_external_id: str, ticket_data: Optional[Dict[str, Any]] = None
    ) -> str:
        ticket = ticket_data or self.get_ticket(ticket_id)
        actual = str(ticket.get("external_id") or "").strip()
        expected = str(expected_external_id).strip()
        if actual != expected:
            raise RuntimeError(
                f"EXTERNAL_ID MISMATCH: Expected '{expected}', Zendesk Ticket {ticket_id} has '{actual}'."
            )
        return actual
