"""Deterministic CRM Account matching for the email requester's Company.

Ported from SalesBoardAutomation (``services/accounts.py`` account-matching-v5 and
``services/requester_identity.py`` requester-identity-v3) so both automations
resolve the same Monday Account from the same requester evidence:

- A unique registrable sender domain wins unless company-name evidence points to
  a different Account.
- Several Accounts sharing that domain need a unique exact company name or the
  sender's own Contact link.
- An approved requester domain alias also needs signature-website agreement.
- An exact normalized company name is used only when the sender has no usable
  business domain. Similar, ambiguous, archived or Duplicate-flagged Accounts
  never become a recommendation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
import hashlib
import json
import logging
import re
import threading
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import tldextract

from .. import monday_client
from .monday_metadata_fields import ACCOUNTS_BOARD_ID


CONTACTS_BOARD_ID = "1654217228"
ACCOUNT_NAME_COLUMN_ID = "name"
ACCOUNT_EMAIL_DOMAIN_COLUMN_ID = "text_mm6bymv5"
ACCOUNT_DUPLICATE_COLUMN_ID = "dropdown_mm6cxq2p"
DUPLICATE_ACCOUNT_LABEL_ID = 1
CONTACT_EMAIL_COLUMN_ID = "contact_email"
CONTACT_ACCOUNTS_COLUMN_ID = "contact_account"
# Each refresh reads the whole Accounts board (three pages); new or renamed
# Accounts reach recommendations within this interval.
INDEX_CACHE_SECONDS = 1800.0
INDEX_RETRY_SECONDS = 60.0
REFRESH_LEASE_SECONDS = 120.0
# Bump when parsing rules or the stored record format change.
SNAPSHOT_FORMAT_VERSION = 1

logger = logging.getLogger(__name__)

AccountMatchReason = Literal[
    "unique_domain", "unique_domain_and_name", "unique_contact_email",
    "unique_domain_alias_website", "unique_exact_name", "not_found_or_ambiguous",
]
MATCH_RATIONALES = {
    "unique_domain": "CRM Account matched by the requester's email domain",
    "unique_domain_and_name": "CRM Account matched by the requester's email domain and exact company name",
    "unique_contact_email": "CRM Account linked to the requester's Contact email",
    "unique_domain_alias_website": "CRM Account matched by an approved domain alias and the requester's website",
    "unique_exact_name": "CRM Account matched by the exact email company name",
}

_LEGAL_SUFFIXES = frozenset({"inc", "incorporated", "limited", "llc", "llp", "ltd", "plc"})
_GENERIC_EMAIL_DOMAINS = frozenset({
    "aol.com", "gmail.com", "googlemail.com", "hotmail.com", "icloud.com", "live.com", "mac.com",
    "me.com", "msn.com", "outlook.com", "proton.me", "protonmail.com", "yahoo.com", "yahoo.co.uk",
})
_SIGNATURE_WEBSITE_LINE = re.compile(r"^\s*(?:w|web|website)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_WEBSITE_CANDIDATE = re.compile(
    r"(?:https?://|www\.)[^\s<>()]+|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?:/[^\s<>()]*)?",
    re.IGNORECASE,
)
_MAX_SIGNATURE_WEBSITE_LINE_LENGTH = 8_192
_MAX_WRAPPED_URL_DEPTH = 2
# The bundled public-suffix snapshot keeps matching offline and deterministic.
_DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

_ACCOUNT_ITEM_FIELDS = """
    id name state board { id }
    column_values(ids: $columnIds) {
      id type value
      ... on TextValue { text }
      ... on DropdownValue { values { id label } }
    }
"""
_CONTACT_ITEM_FIELDS = """
    id name state board { id }
    column_values(ids: $columnIds) {
      id type value
      ... on EmailValue { email text }
      ... on BoardRelationValue { linked_item_ids }
    }
"""
_DIRECTORIES = {
    "Accounts": (ACCOUNTS_BOARD_ID, [ACCOUNT_EMAIL_DOMAIN_COLUMN_ID, ACCOUNT_DUPLICATE_COLUMN_ID], _ACCOUNT_ITEM_FIELDS),
    "Contacts": (CONTACTS_BOARD_ID, [CONTACT_EMAIL_COLUMN_ID, CONTACT_ACCOUNTS_COLUMN_ID], _CONTACT_ITEM_FIELDS),
}


class AccountsContractError(ValueError):
    """Monday returned an unsafe Accounts or Contacts data shape."""


@dataclass(frozen=True)
class AccountRecord:
    item_id: str
    name: str
    active: bool
    email_domain: str | None
    duplicate_label_ids: tuple[int, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.active and DUPLICATE_ACCOUNT_LABEL_ID not in self.duplicate_label_ids


@dataclass(frozen=True)
class AccountsIndex:
    accounts: tuple[AccountRecord, ...]

    @property
    def eligible_accounts(self) -> tuple[AccountRecord, ...]:
        return tuple(account for account in self.accounts if account.eligible)


@dataclass(frozen=True)
class ContactRecord:
    item_id: str
    active: bool
    email_address: str | None
    account_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ContactsIndex:
    contacts: tuple[ContactRecord, ...]

    def matching_email(self, email_address: str) -> tuple[ContactRecord, ...]:
        return tuple(contact for contact in self.contacts
                     if contact.active and contact.email_address == email_address)


@dataclass(frozen=True)
class RequesterIdentity:
    email_address: str | None
    domain: str | None
    company: str | None
    website_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountMatchResult:
    account: AccountRecord | None
    reason: AccountMatchReason
    domain_candidate_ids: tuple[str, ...] = ()
    name_candidate_ids: tuple[str, ...] = ()
    contact_item_id: str | None = None
    contact_candidate_ids: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {"reason": self.reason, "accountItemId": self.account.item_id if self.account else None,
                "contactItemId": self.contact_item_id,
                "domainCandidateIds": list(self.domain_candidate_ids),
                "nameCandidateIds": list(self.name_candidate_ids),
                "contactCandidateIds": list(self.contact_candidate_ids)}


def match_account(
    index: AccountsIndex,
    requester: RequesterIdentity,
    *,
    allow_name_fallback: bool = False,
    account_domain_aliases: Mapping[str, Sequence[str]] | None = None,
    contacts: ContactsIndex | None = None,
) -> AccountMatchResult:
    eligible = index.eligible_accounts
    domain = requester.domain
    direct_domain_matches = tuple(account for account in eligible
                                  if domain is not None and account.email_domain == domain)
    aliases = _normalize_account_domain_aliases(account_domain_aliases or {})
    alias_domain_matches = tuple(account for account in eligible
                                 if domain is not None and domain in aliases.get(account.item_id, ()))
    website_domains = frozenset(value for raw in requester.website_domains
                                if (value := normalize_domain(raw)) is not None)
    website_corroborated_alias_matches = tuple(
        account for account in alias_domain_matches
        if account.email_domain is not None and account.email_domain in website_domains)
    domain_matches = tuple(account for account in eligible
                           if account in direct_domain_matches or account in alias_domain_matches)
    company = normalize_account_name(requester.company)
    name_matches = tuple(account for account in eligible
                         if company is not None and normalize_account_name(account.name) == company)
    direct_domain_name_matches = tuple(account for account in direct_domain_matches if account in name_matches)
    email_address = normalize_email_address(requester.email_address) if requester.email_address else None
    contact_matches = (contacts.matching_email(email_address)
                       if contacts is not None and email_address is not None else ())

    def matched(account, reason, contact=None):
        return AccountMatchResult(account, reason, _ids(domain_matches), _ids(name_matches),
                                  contact.item_id if contact else None, _ids(contact_matches))

    if (len(domain_matches) == 1 and domain_matches[0] in direct_domain_matches
            and not _name_evidence_conflicts(domain_matches[0], name_matches)):
        return matched(domain_matches[0], "unique_domain")
    if len(direct_domain_matches) > 1 and len(direct_domain_name_matches) == 1:
        return matched(direct_domain_name_matches[0], "unique_domain_and_name")
    if len(direct_domain_matches) > 1 and len(contact_matches) == 1:
        contact = contact_matches[0]
        linked = tuple(account for account in eligible if account.item_id in contact.account_item_ids)
        if (len(linked) == 1 and linked[0] in direct_domain_matches
                and not _name_evidence_conflicts(linked[0], name_matches)):
            return matched(linked[0], "unique_contact_email", contact)
    if (not direct_domain_matches and len(website_corroborated_alias_matches) == 1
            and not _name_evidence_conflicts(website_corroborated_alias_matches[0], name_matches)):
        return matched(website_corroborated_alias_matches[0], "unique_domain_alias_website")
    if domain is None and len(name_matches) == 1:
        return matched(name_matches[0], "unique_exact_name")
    if not domain_matches and len(name_matches) == 1 and allow_name_fallback:
        return matched(name_matches[0], "unique_exact_name")
    return AccountMatchResult(None, "not_found_or_ambiguous", _ids(domain_matches), _ids(name_matches),
                              None, _ids(contact_matches))


def resolve_requester_account(
    load_accounts: Callable[[], AccountsIndex],
    load_contacts: Callable[[str], ContactsIndex] | None,
    requester: RequesterIdentity,
    *,
    account_domain_aliases: Mapping[str, Sequence[str]] | None = None,
) -> AccountMatchResult | None:
    """SalesBoardAutomation's account-matching stage; None without any evidence."""
    if requester.domain is None and normalize_account_name(requester.company) is None:
        return None
    index = load_accounts()
    result = match_account(index, requester, account_domain_aliases=account_domain_aliases)
    # Contacts are read only to separate several Accounts sharing one domain.
    if (result.account is None and load_contacts is not None and requester.domain is not None
            and requester.email_address is not None
            and sum(account.email_domain == requester.domain for account in index.eligible_accounts) > 1):
        result = match_account(index, requester, contacts=load_contacts(requester.email_address),
                               account_domain_aliases=account_domain_aliases)
    return result


def requester_identity(
    message: Mapping[str, Any] | None,
    company: str | None,
    *,
    domain_aliases: Mapping[str, str] | None = None,
) -> RequesterIdentity:
    """Identity of the selected external client request's sender and company."""
    company = normalize_company(company)
    sender = _mailbox((message or {}).get("sender"), _normalize_domain_aliases(domain_aliases or {}))
    if sender is None:
        return RequesterIdentity(email_address=None, domain=None, company=company)
    email_address, domain = sender
    return RequesterIdentity(
        email_address=email_address, domain=None if domain in _GENERIC_EMAIL_DOMAINS else domain,
        company=company, website_domains=signature_website_domains(str(message.get("text") or "")))


def normalize_account_name(value: str | None) -> str | None:
    if value is None:
        return None
    tokens = re.findall(r"[^\W_]+", value.casefold().replace("&", " and "))
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or None


def normalize_company(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    if normalized.casefold() in {"", "n/a", "none", "not provided", "null"}:
        return None
    return normalized


def normalize_domain(value: object) -> str | None:
    host = _normalize_host_domain(value)
    if host is None:
        return None
    return _DOMAIN_EXTRACTOR(host).top_domain_under_public_suffix.casefold() or None


def normalize_email_address(value: object) -> str | None:
    """One exact, lower-cased mailbox with an IDNA-normalized host."""
    addresses = getaddresses([str(value)])
    if len(addresses) != 1:
        return None
    local_part, separator, raw_domain = addresses[0][1].strip().rpartition("@")
    if not separator or not local_part or any(character.isspace() for character in local_part):
        return None
    host = _normalize_host_domain(raw_domain)
    if host is None or normalize_domain(host) is None:
        return None
    return f"{local_part.casefold()}@{host}"


def signature_website_domains(body: str) -> tuple[str, ...]:
    domains: list[str] = []
    for line in body.splitlines():
        if len(line) > _MAX_SIGNATURE_WEBSITE_LINE_LENGTH:
            continue
        match = _SIGNATURE_WEBSITE_LINE.match(line)
        if match is None:
            continue
        for raw_candidate in _WEBSITE_CANDIDATE.findall(match.group(1)):
            domain = _website_candidate_domain(raw_candidate)
            if domain is not None and domain not in domains:
                domains.append(domain)
    return tuple(domains)


def parse_account_item(raw_item: Mapping[str, Any]) -> AccountRecord:
    item_id, name, active, columns = _item_header(raw_item, "Account")
    email_column = _find_column(columns, ACCOUNT_EMAIL_DOMAIN_COLUMN_ID, "text", item_id, "Account")
    duplicate_column = _find_column(columns, ACCOUNT_DUPLICATE_COLUMN_ID, "dropdown", item_id, "Account")
    return AccountRecord(item_id=item_id, name=name, active=active,
                         email_domain=_parse_email_domain(email_column),
                         duplicate_label_ids=_parse_duplicate_label_ids(duplicate_column, item_id))


def parse_contact_item(raw_item: Mapping[str, Any]) -> ContactRecord:
    item_id, _, active, columns = _item_header(raw_item, "Contact")
    email_column = _find_column(columns, CONTACT_EMAIL_COLUMN_ID, "email", item_id, "Contact")
    accounts_column = _find_column(columns, CONTACT_ACCOUNTS_COLUMN_ID, "board_relation", item_id, "Contact")
    return ContactRecord(item_id=item_id, active=active, email_address=_parse_contact_email(email_column),
                         account_item_ids=_parse_linked_account_ids(accounts_column, item_id))


def load_directory_index(read_page: Callable[[str | None], Mapping[str, Any]], kind: str):
    """Fully paginated, duplicate-safe Accounts or Contacts index."""
    parse = parse_account_item if kind == "Accounts" else parse_contact_item
    records, seen_ids, seen_cursors, cursor = [], set(), set(), None
    while True:
        page = read_page(cursor)
        items = page.get("items") if isinstance(page, Mapping) else None
        if not isinstance(items, list):
            raise AccountsContractError(f"{kind} page items must be a list")
        for raw_item in items:
            if not isinstance(raw_item, Mapping):
                raise AccountsContractError(f"{kind} item must be an object")
            record = parse(raw_item)
            if record.item_id in seen_ids:
                raise AccountsContractError(f"Monday returned duplicate {kind} item {record.item_id}")
            seen_ids.add(record.item_id)
            records.append(record)
        cursor = page.get("cursor")
        if cursor is None:
            return AccountsIndex(tuple(records)) if kind == "Accounts" else ContactsIndex(tuple(records))
        if not isinstance(cursor, str) or not cursor.strip():
            raise AccountsContractError(f"{kind} page cursor is malformed")
        if cursor in seen_cursors:
            raise AccountsContractError(f"{kind} pagination cursor repeated")
        seen_cursors.add(cursor)


def read_directory_page(access_token: str, kind: str, cursor: str | None = None, *,
                        rules: list[dict[str, Any]] | None = None) -> Mapping[str, Any]:
    board_id, column_ids, fields = _DIRECTORIES[kind]
    variables: dict[str, Any] = {"columnIds": column_ids, "limit": 500}
    if cursor is None:
        # A filtered first page keeps its rules in the cursor for later pages.
        query = ("query ($boardIds: [ID!], $columnIds: [String!], $limit: Int!"
                 + (", $queryParams: ItemsQuery" if rules else "") + ") {"
                 " boards(ids: $boardIds) { id items_page(limit: $limit"
                 + (", query_params: $queryParams" if rules else "") + ") { cursor items { FIELDS } } } }")
        variables["boardIds"] = [board_id]
        if rules:
            variables["queryParams"] = {"rules": rules}
    else:
        query = ("query ($cursor: String!, $columnIds: [String!], $limit: Int!) {"
                 " next_items_page(cursor: $cursor, limit: $limit) { cursor items { FIELDS } } }")
        variables["cursor"] = cursor
    payload = monday_client.monday_graphql_request(
        access_token, query.replace("FIELDS", fields), variables, timeout=30)
    if not isinstance(payload, Mapping) or payload.get("errors"):
        raise AccountsContractError(f"Monday {kind} page is unavailable")
    data = payload.get("data") or {}
    if cursor is None:
        boards = data.get("boards")
        if not isinstance(boards, list) or len(boards) != 1 or not isinstance(boards[0], Mapping):
            raise AccountsContractError(f"Monday returned an unexpected {kind} board")
        page = boards[0].get("items_page")
    else:
        page = data.get("next_items_page")
    if not isinstance(page, Mapping):
        raise AccountsContractError(f"Monday returned a malformed {kind} page")
    return page


def load_contacts_for_email(access_token: str, email_address: str | None) -> ContactsIndex:
    """Contacts that may hold the sender's address, instead of scanning the whole board.

    Monday's ``contains_text`` matches the address or its display label regardless
    of case, so every exact holder (including duplicates) is returned; the matcher
    keeps only exact addresses.
    """
    email = normalize_email_address(email_address) if email_address else None
    if email is None:
        return ContactsIndex(())
    rules = [{"column_id": CONTACT_EMAIL_COLUMN_ID, "compare_value": email, "operator": "contains_text"}]
    return load_directory_index(
        lambda cursor: read_directory_page(access_token, "Contacts", cursor, rules=rules), "Contacts")


def directory_scope(access_token: str) -> str:
    """Isolate Monday sources and permission scopes without persisting credentials."""
    from ..config import settings
    return _digest([monday_client.MONDAY_API_URL, settings.monday_api_version, access_token])


def encode_accounts_index(index: AccountsIndex) -> dict[str, Any]:
    return {"accounts": [[account.item_id, account.name, account.active, account.email_domain,
                          list(account.duplicate_label_ids)] for account in index.accounts]}


def decode_accounts_index(payload: object) -> AccountsIndex:
    rows = payload.get("accounts") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise AccountsContractError("Stored Accounts snapshot is malformed")
    accounts = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            raise AccountsContractError("Stored Accounts snapshot is malformed")
        item_id, name, active, domain, labels = row
        if (not isinstance(item_id, str) or not item_id.isdecimal() or not isinstance(name, str) or not name
                or not isinstance(active, bool) or not (domain is None or isinstance(domain, str))
                or not isinstance(labels, list)
                or any(isinstance(label, bool) or not isinstance(label, int) for label in labels)):
            raise AccountsContractError("Stored Accounts snapshot is malformed")
        accounts.append(AccountRecord(item_id, name, active, domain, tuple(labels)))
    return AccountsIndex(tuple(accounts))


@dataclass(frozen=True)
class Snapshot:
    index: AccountsIndex
    refreshed_at: datetime


class SnapshotStore:
    """Durable Accounts snapshot shared by the API and every worker process.

    Monday is never read inside a database transaction: a short refresh lease lets
    one process refresh while the others keep serving the stored copy.
    """

    def __init__(self, session_factory, scope: str, kind: str = "Accounts"):
        self._session_factory, self._kind, self._board_id = session_factory, kind, _DIRECTORIES[kind][0]
        self._key = _digest([SNAPSHOT_FORMAT_VERSION, scope, kind, self._board_id])

    def read(self) -> Snapshot | None:
        from ..models import CRMDirectorySnapshot
        with self._session_factory() as db:
            row = db.get(CRMDirectorySnapshot, self._key)
            if row is None or row.payload_json is None or row.refreshed_at is None:
                return None
            return Snapshot(decode_accounts_index(row.payload_json), _aware(row.refreshed_at))

    def claim_refresh(self, now: datetime, *, ttl_seconds: float, lease_seconds: float) -> bool:
        """Take the lease unless the stored copy is fresh or another refresh is live."""
        from ..models import CRMDirectorySnapshot
        with self._session_factory() as db:
            db.execute(_insert(db, CRMDirectorySnapshot).values(
                cache_key=self._key, kind=self._kind, board_id=self._board_id,
            ).on_conflict_do_nothing(index_elements=["cache_key"]))
            row = db.query(CRMDirectorySnapshot).filter_by(cache_key=self._key).populate_existing().with_for_update().one()
            if ((row.payload_json is not None and _is_fresh(row.refreshed_at, now, ttl_seconds))
                    or (row.refresh_lease_until is not None and _aware(row.refresh_lease_until) > now)):
                db.rollback()
                return False
            row.refresh_lease_until = now + timedelta(seconds=lease_seconds)
            db.commit()
            return True

    def release_refresh(self) -> None:
        from ..models import CRMDirectorySnapshot
        with self._session_factory() as db:
            db.query(CRMDirectorySnapshot).filter_by(cache_key=self._key).update({"refresh_lease_until": None})
            db.commit()

    def write(self, index: AccountsIndex, refreshed_at: datetime) -> None:
        from ..models import CRMDirectorySnapshot
        with self._session_factory() as db:
            statement = _insert(db, CRMDirectorySnapshot).values(
                cache_key=self._key, kind=self._kind, board_id=self._board_id,
                payload_json=encode_accounts_index(index), refreshed_at=refreshed_at, refresh_lease_until=None)
            db.execute(statement.on_conflict_do_update(index_elements=["cache_key"], set_={
                "payload_json": statement.excluded.payload_json, "refreshed_at": statement.excluded.refreshed_at,
                "refresh_lease_until": None}))
            db.commit()


@dataclass
class _Entry:
    index: AccountsIndex | None = None
    refreshed_at: datetime | None = None
    next_check: datetime = datetime.min.replace(tzinfo=timezone.utc)
    error: BaseException | None = None


class DirectoryCache:
    """Process memory in front of the shared snapshot (stale-while-revalidate).

    A fresh copy comes from memory. An expired copy is still served while one
    background refresh runs, so reviews do not wait for Monday. A failed refresh
    keeps serving the last copy and retries after a short backoff: a transient
    outage must not flip recommendations (and their source identity). Only a cold
    start with no stored copy reads Monday on the request path.
    """

    def __init__(self, *, ttl_seconds: float = INDEX_CACHE_SECONDS, retry_seconds: float = INDEX_RETRY_SECONDS,
                 lease_seconds: float = REFRESH_LEASE_SECONDS, now: Callable[[], datetime] | None = None,
                 spawn: Callable[[Callable[[], Any]], Any] | None = None):
        self._ttl_seconds, self._retry_seconds, self._lease_seconds = ttl_seconds, retry_seconds, lease_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._spawn = spawn or (lambda target: threading.Thread(
            target=target, name="crm-directory-refresh", daemon=True).start())
        self._lock = threading.Lock()
        self._entries: dict[Any, _Entry] = {}
        self._key_locks: dict[Any, threading.Lock] = {}
        self._refreshing: set[Any] = set()

    def load(self, key: Any, fetch: Callable[[], AccountsIndex], store: SnapshotStore | None = None) -> AccountsIndex:
        now = self._now()
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            if entry.index is not None and (_is_fresh(entry.refreshed_at, now, self._ttl_seconds)
                                            or now < entry.next_check):
                return entry.index
            if entry.error is not None and now < entry.next_check:
                raise AccountsContractError("CRM Accounts directory is unavailable") from entry.error
        stored = self._store(store, "read")
        with self._lock:
            entry = self._entries[key]
            if stored is not None and (entry.refreshed_at is None or stored.refreshed_at > entry.refreshed_at):
                entry.index, entry.refreshed_at, entry.error = stored.index, stored.refreshed_at, None
            index, start = entry.index, False
            if index is not None and not _is_fresh(entry.refreshed_at, now, self._ttl_seconds):
                entry.next_check = now + timedelta(seconds=self._retry_seconds)
                start = key not in self._refreshing
                self._refreshing.add(key)
        if index is None:
            return self._refresh(key, fetch, store, cold=True)
        if start:
            self._spawn(lambda: self._refresh(key, fetch, store))
        return index

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _refresh(self, key, fetch, store, *, cold=False):
        try:
            with self._key_lock(key):
                with self._lock:
                    entry = self._entries[key]
                    if cold and entry.index is not None:
                        return entry.index  # Another thread completed the cold load.
                    if cold and entry.error is not None and self._now() < entry.next_check:
                        # A queued caller shares the failed cold load's backoff.
                        raise AccountsContractError("CRM Accounts directory is unavailable") from entry.error
                return self._fetch_and_store(key, fetch, store, cold=cold)
        finally:
            if not cold:
                with self._lock:
                    self._refreshing.discard(key)

    def _fetch_and_store(self, key, fetch, store, *, cold):
        """Runs under the key lock, so queued callers observe its outcome, including failure."""
        claimed = False
        try:
            claimed = self._store(store, "claim_refresh", self._now(), default=True,
                                  ttl_seconds=self._ttl_seconds, lease_seconds=self._lease_seconds)
            if not claimed:
                # Another process refreshed or is refreshing: use its copy.
                stored = self._store(store, "read")
                if stored is not None:
                    return self._remember(key, stored.index, stored.refreshed_at)
                if not cold:
                    return None
            index = fetch()
            refreshed_at = self._now()
            self._store(store, "write", index, refreshed_at)
            claimed = False
            return self._remember(key, index, refreshed_at)
        except Exception as exc:
            if claimed and store is not None:
                self._store(store, "release_refresh")
            logger.warning("CRM Accounts directory refresh failed (%s: %s)%s", type(exc).__name__, exc,
                           "" if cold else "; serving the last copy")
            with self._lock:
                entry = self._entries[key]
                entry.next_check = self._now() + timedelta(seconds=self._retry_seconds)
                if entry.index is None:
                    entry.error = exc
            if cold:
                raise
            return None

    def _remember(self, key, index, refreshed_at):
        with self._lock:
            entry = self._entries[key]
            entry.index, entry.refreshed_at, entry.error = index, refreshed_at, None
        return index

    def _key_lock(self, key) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _store(store, method, *args, default=None, **kwargs):
        """The shared snapshot is an optimization; its failure falls back to Monday."""
        if store is None:
            return default
        try:
            return getattr(store, method)(*args, **kwargs)
        except Exception as exc:
            from .database_diagnostics import database_error_details
            details = database_error_details(exc) or {"error_type": type(exc).__name__}
            logger.warning("CRM Accounts snapshot %s unavailable details=%s", method, json.dumps(details, sort_keys=True))
            return default


DIRECTORY_CACHE = DirectoryCache()


def cached_accounts_index(access_token: str, *, session_factory=None) -> AccountsIndex:
    """Accounts index from process memory, the shared snapshot, or Monday."""
    if session_factory is None:
        from ..db import SessionLocal as session_factory
    scope = directory_scope(access_token)
    return DIRECTORY_CACHE.load((scope, "Accounts"), lambda: load_directory_index(
        lambda cursor: read_directory_page(access_token, "Accounts", cursor), "Accounts"),
        SnapshotStore(session_factory, scope))


def start_directory_warmup(access_token: str, *, session_factory=None) -> threading.Thread:
    """Load the Accounts index in the background so the first review need not wait."""
    def warm():
        try:
            cached_accounts_index(access_token, session_factory=session_factory)
            logger.info("CRM Accounts directory is ready")
        except Exception as exc:
            logger.warning("CRM Accounts directory warm-up failed (%s)", type(exc).__name__)

    thread = threading.Thread(target=warm, name="crm-accounts-warmup", daemon=True)
    thread.start()
    return thread


def _digest(values: list[Any]) -> str:
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _is_fresh(refreshed_at: datetime | None, now: datetime, ttl_seconds: float) -> bool:
    return refreshed_at is not None and (now - _aware(refreshed_at)).total_seconds() < ttl_seconds


def _insert(db, model):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("The CRM directory snapshot requires PostgreSQL")
    return insert(model)


def _ids(records) -> tuple[str, ...]:
    return tuple(record.item_id for record in records)


def _name_evidence_conflicts(domain_match: AccountRecord, name_matches: tuple[AccountRecord, ...]) -> bool:
    return bool(name_matches) and (len(name_matches) != 1 or name_matches[0].item_id != domain_match.item_id)


def _normalize_account_domain_aliases(values: Mapping[str, Sequence[str]]) -> dict[str, frozenset[str]]:
    aliases: dict[str, frozenset[str]] = {}
    for raw_item_id, raw_domains in values.items():
        item_id = str(raw_item_id).strip()
        if not item_id.isdecimal() or int(item_id) <= 0:
            continue
        domains = frozenset(domain for value in ((raw_domains,) if isinstance(raw_domains, str) else raw_domains)
                            if (domain := normalize_domain(value)) is not None)
        if domains:
            aliases[item_id] = domains
    return aliases


def _normalize_domain_aliases(values: Mapping[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for raw_source, raw_target in values.items():
        source, target = _normalize_host_domain(raw_source), normalize_domain(raw_target)
        if source is not None and target is not None:
            aliases[source] = target
    return aliases


def _mailbox(value: object, aliases: Mapping[str, str]) -> tuple[str, str] | None:
    if not value:
        return None
    email_address = normalize_email_address(value)
    if email_address is None:
        return None
    host = email_address.rpartition("@")[2]
    domain = normalize_domain(host)
    if domain is None:
        return None
    return email_address, aliases.get(host, domain)


def _normalize_host_domain(value: object) -> str | None:
    candidate = str(value).strip().casefold().rstrip(".")
    if candidate.startswith("www."):
        candidate = candidate[4:]
    if not candidate or "@" in candidate or any(character.isspace() for character in candidate):
        return None
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _website_candidate_domain(raw_candidate: str, *, depth: int = 0) -> str | None:
    if depth > _MAX_WRAPPED_URL_DEPTH:
        return None
    candidate = raw_candidate.strip().rstrip(".,;:!?]}\"'")
    if not candidate:
        return None
    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or host is None:
        return None
    host = _normalize_host_domain(host)
    if host is None:
        return None
    wrapped_parameter = ("a" if host == "linkprotect.cudasvc.com" else "url"
                         if host == "safelinks.protection.outlook.com"
                         or host.endswith(".safelinks.protection.outlook.com") else None)
    if wrapped_parameter is not None:
        try:
            targets = parse_qs(parsed.query, keep_blank_values=False, max_num_fields=32).get(wrapped_parameter, [])
        except ValueError:
            return None
        return _website_candidate_domain(targets[0], depth=depth + 1) if len(targets) == 1 else None
    return normalize_domain(host)


def _item_header(raw_item: Mapping[str, Any], kind: str) -> tuple[str, str, bool, list[Any]]:
    item_id = str(raw_item.get("id", "")).strip()
    if not item_id.isdecimal() or int(item_id) <= 0:
        raise AccountsContractError(f"{kind} item ID must be a positive decimal")
    name = raw_item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AccountsContractError(f"{kind} item {item_id} has no name")
    state = raw_item.get("state")
    if not isinstance(state, str):
        raise AccountsContractError(f"{kind} item {item_id} has no typed state")
    columns = raw_item.get("column_values")
    if not isinstance(columns, list):
        raise AccountsContractError(f"{kind} item {item_id} column_values must be a list")
    return item_id, " ".join(name.split()), state.casefold() == "active", columns


def _find_column(columns: list[Any], column_id: str, expected_type: str, item_id: str, kind: str) -> Mapping[str, Any]:
    matches = [column for column in columns if isinstance(column, Mapping) and column.get("id") == column_id]
    if len(matches) != 1 or matches[0].get("type") != expected_type:
        raise AccountsContractError(f"{kind} item {item_id} has an invalid {column_id} column")
    return matches[0]


def _parse_email_domain(column: Mapping[str, Any]) -> str | None:
    raw_text = column.get("text")
    if raw_text is None and "value" in column:
        raw_text = _decode_json_value(column.get("value"))
    if raw_text is None or raw_text == "":
        return None
    return normalize_domain(raw_text)


def _parse_contact_email(column: Mapping[str, Any]) -> str | None:
    raw_email = column.get("email")
    if raw_email is None and "value" in column:
        decoded = _decode_json_value(column.get("value"))
        if isinstance(decoded, Mapping):
            raw_email = decoded.get("email")
        elif decoded is not None:
            return None
    if raw_email is None or raw_email == "":
        return None
    return normalize_email_address(raw_email)


def _parse_linked_account_ids(column: Mapping[str, Any], item_id: str) -> tuple[str, ...]:
    raw_ids = column.get("linked_item_ids")
    if not isinstance(raw_ids, list):
        raise AccountsContractError(f"Contact item {item_id} Accounts links must be a list")
    account_ids: list[str] = []
    for raw_id in raw_ids:
        account_id = str(raw_id).strip()
        if isinstance(raw_id, bool) or not account_id.isdecimal() or int(account_id) <= 0:
            raise AccountsContractError(f"Contact item {item_id} Account link ID is malformed")
        if account_id not in account_ids:
            account_ids.append(account_id)
    return tuple(account_ids)


def _parse_duplicate_label_ids(column: Mapping[str, Any], item_id: str) -> tuple[int, ...]:
    if "values" in column:
        values = column.get("values")
        if values is None:
            return ()
        if not isinstance(values, list):
            raise AccountsContractError(f"Account item {item_id} Duplicate values must be a list")
        raw_ids = [value.get("id") if isinstance(value, Mapping) else None for value in values]
    else:
        decoded = _decode_json_value(column.get("value"))
        if decoded is None:
            return ()
        if not isinstance(decoded, Mapping) or not isinstance(decoded.get("ids"), list):
            raise AccountsContractError(f"Account item {item_id} Duplicate value is malformed")
        raw_ids = decoded["ids"]
    label_ids: list[int] = []
    for raw_id in raw_ids:
        try:
            if isinstance(raw_id, bool):
                raise TypeError
            label_id = int(raw_id)
        except (TypeError, ValueError) as error:
            raise AccountsContractError(f"Account item {item_id} Duplicate label ID is malformed") from error
        if label_id <= 0:
            raise AccountsContractError(f"Account item {item_id} Duplicate label ID is malformed")
        if label_id not in label_ids:
            label_ids.append(label_id)
    return tuple(sorted(label_ids))


def _decode_json_value(value: object) -> object:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise AccountsContractError("Monday column value contains malformed JSON") from error
