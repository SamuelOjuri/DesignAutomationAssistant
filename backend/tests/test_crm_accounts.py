"""SalesBoardAutomation's requester-to-Account matching rules, ported for Company."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib
import time

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from backend.app.config import settings
from backend.app.models import CRMDirectorySnapshot
from backend.app.services import crm_accounts
from backend.app.services.crm_accounts import (
    AccountRecord, AccountsContractError, AccountsIndex, ContactRecord, ContactsIndex, DirectoryCache,
    INDEX_CACHE_SECONDS, RequesterIdentity, SnapshotStore, load_contacts_for_email, load_directory_index,
    match_account, requester_identity,
)


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
EXPIRED = NOW - timedelta(seconds=INDEX_CACHE_SECONDS + 1)
MIGRATION = importlib.import_module("backend.migrations.versions.0023_crm_directory_snapshots")


def record(item_id, name, domain=None, *, active=True, duplicate=False):
    return AccountRecord(item_id, name, active, domain, (1,) if duplicate else ())


def requester(domain=None, company=None, email=None, websites=()):
    return RequesterIdentity(email, domain, company, websites)


def test_unique_domain_wins_unless_company_name_identifies_another_account():
    index = AccountsIndex((record("10", "Axter Limited", "axter.co.uk"), record("20", "Bauder Ltd", "bauder.co.uk")))

    matched = match_account(index, requester("axter.co.uk", "Axter Ltd"))
    conflicted = match_account(index, requester("axter.co.uk", "Bauder"))

    assert (matched.account.item_id, matched.reason) == ("10", "unique_domain")
    assert conflicted.account is None
    assert (conflicted.domain_candidate_ids, conflicted.name_candidate_ids) == (("10",), ("20",))


def test_shared_domain_needs_a_unique_exact_name_or_the_senders_contact():
    index = AccountsIndex((record("10", "Acme North", "acme.co.uk"), record("20", "Acme South", "acme.co.uk")))
    contacts = ContactsIndex((ContactRecord("90", True, "jo@acme.co.uk", ("20",)),
                              ContactRecord("91", False, "old@acme.co.uk", ("10",))))

    assert match_account(index, requester("acme.co.uk", "ACME South Ltd")).reason == "unique_domain_and_name"
    assert match_account(index, requester("acme.co.uk", "Acme")).account is None
    by_contact = match_account(index, requester("acme.co.uk", email="Jo@Acme.co.uk"), contacts=contacts)
    assert (by_contact.account.item_id, by_contact.reason) == ("20", "unique_contact_email")
    assert match_account(index, requester("acme.co.uk", email="old@acme.co.uk"), contacts=contacts).account is None


def test_exact_name_is_used_only_without_a_business_domain_and_never_for_ineligible_accounts():
    index = AccountsIndex((record("10", "Kingsgate Construction Ltd"),
                           record("11", "Kingsgate Construction", duplicate=True),
                           record("12", "Old Roofing Co", active=False)))

    assert match_account(index, requester(None, "KINGSGATE CONSTRUCTION LIMITED")).reason == "unique_exact_name"
    assert match_account(index, requester("kingsgate.co.uk", "Kingsgate Construction")).account is None
    assert match_account(index, requester(None, "Kingsgates Construction")).account is None
    assert match_account(index, requester(None, "Old Roofing Co")).account is None


def test_approved_account_domain_alias_requires_signature_website_agreement():
    index = AccountsIndex((record("1661824807", "AccuRoof", "accuroof.co.uk"),))
    aliases = {"1661824807": ["sigplc.com"]}

    agreed = match_account(index, requester("sigplc.com", websites=("www.accuroof.co.uk",)),
                           account_domain_aliases=aliases)

    assert agreed.reason == "unique_domain_alias_website"
    assert match_account(index, requester("sigplc.com"), account_domain_aliases=aliases).account is None


def test_requester_identity_uses_selected_sender_domain_aliases_and_wrapped_signature_website():
    message = {"sender": "jo@mail.axter.co.uk", "text": (
        "From: Jo <jo@mail.axter.co.uk>\nKind regards\n"
        "W: https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.axter.co.uk%2F&data=1")}

    assert requester_identity(message, "  Axter   Ltd ") == RequesterIdentity(
        "jo@mail.axter.co.uk", "axter.co.uk", "Axter Ltd", ("axter.co.uk",))
    assert requester_identity({"sender": "jo@gmail.com"}, "Not Provided") == RequesterIdentity(
        "jo@gmail.com", None, None)
    assert requester_identity({"sender": "help@tremcocpgsupport.zendesk.com"}, None,
        domain_aliases={"tremcocpgsupport.zendesk.com": "tremcocpg.com"}).domain == "tremcocpg.com"
    assert requester_identity(None, "Axter Ltd") == RequesterIdentity(None, None, "Axter Ltd")


def account_item(item_id, domain, duplicate_ids=(), state="active"):
    return {"id": item_id, "name": f" Account   {item_id} ", "state": state, "board": {"id": "1654217230"},
            "column_values": [
                {"id": "text_mm6bymv5", "type": "text", "text": domain, "value": None},
                {"id": "dropdown_mm6cxq2p", "type": "dropdown", "value": None,
                 "values": [{"id": label_id, "label": "Flag"} for label_id in duplicate_ids]},
            ]}


def test_directory_index_paginates_filters_duplicates_and_rejects_repeated_cursors():
    pages = {None: {"cursor": "next", "items": [account_item("1", "www.Axter.co.uk"), account_item("2", "", [1])]},
             "next": {"cursor": None, "items": [account_item("3", "acme.co.uk", [2]),
                                                account_item("4", "old.co.uk", state="archived")]}}

    index = load_directory_index(pages.__getitem__, "Accounts")

    assert [(account.item_id, account.name, account.email_domain, account.eligible) for account in index.accounts] == [
        ("1", "Account 1", "axter.co.uk", True), ("2", "Account 2", None, False),
        ("3", "Account 3", "acme.co.uk", True), ("4", "Account 4", "old.co.uk", False)]
    pages["next"]["cursor"] = "next"
    with pytest.raises(AccountsContractError, match="cursor repeated"):
        load_directory_index(pages.__getitem__, "Accounts")


@pytest.fixture
def snapshot_sessions(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "crm.sqlite"))
    CRMDirectorySnapshot.__table__.create(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def directory_cache(clock, spawned=None):
    return DirectoryCache(now=lambda: clock[0], spawn=spawned.append if spawned is not None else lambda target: target())


def unavailable(message="Monday Accounts page is unavailable"):
    def fetch():
        raise AccountsContractError(message)
    return fetch


def test_shared_snapshot_lets_a_restarted_or_other_process_skip_monday(snapshot_sessions):
    clock = [NOW]
    index = AccountsIndex((record("701", "Axter Limited", "axterltd.co.uk"), record("702", "Old", duplicate=True)))
    assert directory_cache(clock).load("key", lambda: index, SnapshotStore(snapshot_sessions, "scope")) is index

    clock[0] = NOW + timedelta(seconds=INDEX_CACHE_SECONDS - 1)
    restarted = directory_cache(clock).load(
        "key", lambda: pytest.fail("Monday was read despite a fresh shared snapshot"), SnapshotStore(snapshot_sessions, "scope"))

    assert restarted == index
    assert SnapshotStore(snapshot_sessions, "another-token-scope").read() is None


def test_expired_copy_is_served_while_one_background_refresh_updates_the_snapshot(snapshot_sessions):
    clock, spawned = [NOW], []
    old, new = AccountsIndex((record("1", "Old Name"),)), AccountsIndex((record("1", "New Name"),))
    store = SnapshotStore(snapshot_sessions, "scope")
    store.write(old, EXPIRED)
    cache = directory_cache(clock, spawned)

    assert cache.load("key", lambda: new, store) == old
    assert cache.load("key", lambda: pytest.fail("A second refresh started"), store) == old
    assert len(spawned) == 1
    spawned[0]()

    assert cache.load("key", lambda: pytest.fail("A fresh copy was refreshed"), store) is new
    assert (store.read().index, store.read().refreshed_at) == (new, NOW)


def test_refresh_lease_admits_one_process_until_it_expires_or_is_released(snapshot_sessions):
    store = SnapshotStore(snapshot_sessions, "scope")

    def claim(seconds):
        return store.claim_refresh(NOW + timedelta(seconds=seconds), ttl_seconds=300, lease_seconds=120)

    assert claim(0) is True
    assert claim(119) is False
    assert claim(121) is True
    store.release_refresh()
    assert claim(122) is True
    store.write(AccountsIndex(()), NOW + timedelta(seconds=123))
    assert claim(124) is False


def test_failed_refresh_serves_the_last_copy_releases_the_lease_and_backs_off(snapshot_sessions):
    clock = [NOW]
    old = AccountsIndex((record("1", "Old Name"),))
    store = SnapshotStore(snapshot_sessions, "scope")
    store.write(old, EXPIRED)
    cache = directory_cache(clock)

    assert cache.load("key", unavailable(), store) == old
    assert cache.load("key", lambda: pytest.fail("Retried during backoff"), store) == old
    assert store.claim_refresh(NOW, ttl_seconds=300, lease_seconds=120) is True


def test_cold_failure_backs_off_and_an_unavailable_snapshot_falls_back_to_monday():
    clock = [NOW]
    cache = directory_cache(clock)
    with pytest.raises(AccountsContractError, match="page is unavailable"):
        cache.load("key", unavailable())
    with pytest.raises(AccountsContractError, match="directory is unavailable"):
        cache.load("key", lambda: pytest.fail("Retried during backoff"))
    clock[0] = NOW + timedelta(seconds=60)
    missing_table = SnapshotStore(sessionmaker(bind=create_engine("sqlite://")), "scope")
    index = AccountsIndex(())

    assert cache.load("key", lambda: index, missing_table) is index


def test_concurrent_cold_callers_share_one_failed_monday_load():
    calls, cache = [], DirectoryCache()

    def fetch():
        calls.append(1)
        time.sleep(0.3)  # Let the other callers queue on the key lock.
        raise AccountsContractError("Monday Accounts page is unavailable")

    def attempt(_):
        try:
            cache.load("key", fetch)
        except AccountsContractError as exc:
            return str(exc)

    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(attempt, range(4)))

    assert len(calls) == 1
    assert sorted(outcomes) == ["CRM Accounts directory is unavailable"] * 3 + ["Monday Accounts page is unavailable"]


def test_sender_contacts_use_a_server_side_filter_and_exact_address_matching(monkeypatch):
    calls = []

    def contact(item_id, email, account_ids):
        return {"id": item_id, "name": item_id, "state": "active", "board": {"id": "1654217228"}, "column_values": [
            {"id": "contact_email", "type": "email", "email": email, "text": email, "value": None},
            {"id": "contact_account", "type": "board_relation", "linked_item_ids": account_ids, "value": None}]}

    def request(access_token, query, variables, timeout):
        calls.append((query, variables))
        return {"data": {"boards": [{"items_page": {"cursor": None, "items": [
            contact("90", "Jo@AxterLtd.co.uk", ["701", 701]), contact("91", "bjo@axterltd.co.uk", ["702"])]}}]}}

    monkeypatch.setattr(crm_accounts.monday_client, "monday_graphql_request", request)
    contacts = load_contacts_for_email("token", "jo@axterltd.co.uk")
    index = AccountsIndex((record("701", "Axter Limited", "axterltd.co.uk"),
                           record("702", "Axter Limited (DAAS)", "axterltd.co.uk")))

    result = match_account(index, requester("axterltd.co.uk", email="jo@axterltd.co.uk"), contacts=contacts)

    assert "query_params: $queryParams" in calls[0][0]
    assert calls[0][1]["queryParams"] == {"rules": [
        {"column_id": "contact_email", "compare_value": "jo@axterltd.co.uk", "operator": "contains_text"}]}
    assert contacts.contacts[0] == ContactRecord("90", True, "jo@axterltd.co.uk", ("701",))
    assert (result.account.item_id, result.contact_item_id) == ("701", "90")
    assert load_contacts_for_email("token", None) == ContactsIndex(()) and len(calls) == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_api_startup_warms_the_accounts_directory_only_when_reviews_are_enabled(monkeypatch, enabled):
    from backend.app import main

    started = []
    monkeypatch.setattr(main, "start_directory_warmup", started.append)
    monkeypatch.setattr(settings, "performance_specification_review_enabled", enabled)
    monkeypatch.setattr(settings, "monday_ingestion_access_token", "token")
    with TestClient(main.app):
        pass

    assert started == (["token"] if enabled else [])


def test_directory_warmup_runs_in_the_background_and_tolerates_failure(monkeypatch):
    calls = []

    def load(access_token, *, session_factory=None):
        calls.append(access_token)
        raise AccountsContractError("Monday Accounts page is unavailable")

    monkeypatch.setattr(crm_accounts, "cached_accounts_index", load)
    thread = crm_accounts.start_directory_warmup("token")
    thread.join(timeout=5)

    assert thread.daemon and not thread.is_alive() and calls == ["token"]


def test_snapshot_migration_round_trip_matches_the_model(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "migration.sqlite"))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
            assert {column["name"] for column in inspect(connection).get_columns("crm_directory_snapshots")} == set(
                CRMDirectorySnapshot.__table__.columns.keys())
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.downgrade()
            assert inspect(connection).get_table_names() == []
    finally:
        engine.dispose()
