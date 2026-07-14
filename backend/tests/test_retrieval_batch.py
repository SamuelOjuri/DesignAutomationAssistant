from types import SimpleNamespace

from backend.app.services import retrieval


def _candidate(
    chunk_id: str,
    query_index: int,
    score: float,
    *,
    file_id: str | None = None,
) -> dict:
    return {
        "chunkId": chunk_id,
        "fileId": file_id or f"file-{chunk_id}",
        "filename": f"{chunk_id}.txt",
        "snippet": f"content for {chunk_id}",
        "score": score,
        "matchedQuery": f"query-{query_index}",
        "matchedQueryIndex": query_index,
    }


def test_search_task_docs_batch_embeds_queries_once_and_reuses_latest_snapshot(
    monkeypatch,
):
    snapshot_calls = []
    embed_calls = []
    search_calls = []
    snapshot = SimpleNamespace(id="latest-snapshot")

    def fake_latest_snapshot(db, external_task_key):
        snapshot_calls.append((db, external_task_key))
        return snapshot

    class FakeModels:
        def embed_content(self, *, model, contents, config):
            embed_calls.append(
                {"model": model, "contents": contents, "config": config}
            )
            return SimpleNamespace(
                embeddings=[
                    SimpleNamespace(values=[3.0, 4.0]),
                    SimpleNamespace(values=[0.0, 2.0]),
                ]
            )

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    def fake_search_snapshot(
        db,
        external_task_key,
        snapshot_id,
        query,
        query_index,
        query_vec,
        k,
    ):
        search_calls.append(
            {
                "db": db,
                "external_task_key": external_task_key,
                "snapshot_id": snapshot_id,
                "query": query,
                "query_index": query_index,
                "query_vec": query_vec,
                "k": k,
            }
        )
        return [_candidate(f"chunk-{query_index}", query_index, 0.1)]

    monkeypatch.setattr(retrieval, "_latest_snapshot", fake_latest_snapshot)
    monkeypatch.setattr(retrieval.genai, "Client", FakeClient)
    monkeypatch.setattr(
        retrieval,
        "_search_snapshot_for_embedding",
        fake_search_snapshot,
    )

    results = retrieval.search_task_docs_batch(
        db="db",
        external_task_key="acct:board:item",
        queries=["  first query  ", "second query"],
        k=20,
    )

    assert snapshot_calls == [("db", "acct:board:item")]
    assert len(embed_calls) == 1
    assert embed_calls[0]["model"] == "gemini-embedding-001"
    assert embed_calls[0]["contents"] == ["first query", "second query"]
    assert [call["snapshot_id"] for call in search_calls] == [
        "latest-snapshot",
        "latest-snapshot",
    ]
    assert [call["query_vec"] for call in search_calls] == [
        [0.6, 0.8],
        [0.0, 1.0],
    ]
    assert [call["k"] for call in search_calls] == [8, 8]
    assert [result["chunkId"] for result in results] == ["chunk-0", "chunk-1"]


def test_select_diverse_evidence_deduplicates_by_best_score_and_round_robins():
    candidates = [
        _candidate("shared", 0, 0.4, file_id="file-shared"),
        _candidate("query-0-a", 0, 0.2, file_id="file-a"),
        _candidate("query-0-b", 0, 0.3, file_id="file-a"),
        _candidate("shared", 1, 0.1, file_id="file-shared"),
        _candidate("query-1-a", 1, 0.2, file_id="file-b"),
        _candidate("query-1-b", 1, 0.3, file_id="file-c"),
        _candidate("query-2-a", 2, 0.2, file_id="file-d"),
    ]

    selected = retrieval.select_diverse_evidence(
        candidates,
        max_evidence_chunks=6,
        max_chunks_per_file=1,
    )

    assert [result["chunkId"] for result in selected] == [
        "shared",
        "query-1-a",
        "query-2-a",
        "query-0-a",
        "query-1-b",
    ]
    assert selected[0]["score"] == 0.1
    assert selected[0]["matchedQueryIndexes"] == [0, 1]
    assert selected[0]["selectedByQueryIndex"] == 0
    assert {result["selectedByQueryIndex"] for result in selected[:3]} == {0, 1, 2}
    assert all(
        sum(result["fileId"] == file_id for result in selected) <= 1
        for file_id in {result["fileId"] for result in selected}
    )

    capped = retrieval.select_diverse_evidence(
        candidates,
        max_evidence_chunks=4,
        max_chunks_per_file=3,
    )
    assert len(capped) == 4


def test_select_diverse_evidence_cannot_exceed_configured_caps():
    candidates = [
        _candidate(
            f"{file_id}-{chunk_index}",
            0,
            chunk_index / 10,
            file_id=file_id,
        )
        for file_id in ("file-a", "file-b", "file-c", "file-d", "file-e")
        for chunk_index in range(5)
    ]

    selected = retrieval.select_diverse_evidence(
        candidates,
        max_evidence_chunks=100,
        max_chunks_per_file=100,
    )

    assert len(selected) == 12
    assert all(
        sum(result["fileId"] == file_id for result in selected) <= 3
        for file_id in {result["fileId"] for result in selected}
    )


def test_search_task_docs_wrapper_preserves_public_citation_shape(monkeypatch):
    batch_calls = []

    def fake_batch(db, external_task_key, queries, k, **kwargs):
        batch_calls.append(
            {
                "queries": queries,
                "k": k,
                **kwargs,
            }
        )
        return [
            {
                "chunkId": "chunk-1",
                "filename": "source.pdf",
                "page": 2,
                "section": "roof",
                "snippet": "Roof details",
                "score": 0.1,
                "fileId": "file-1",
                "mondayAssetId": "asset-1",
                "matchedQuery": "roof",
                "matchedQueryIndex": 0,
                "matchedQueries": ["roof"],
                "matchedQueryIndexes": [0],
                "selectedByQuery": "roof",
                "selectedByQueryIndex": 0,
            }
        ]

    monkeypatch.setattr(retrieval, "search_task_docs_batch", fake_batch)

    results = retrieval.search_task_docs(
        db="db",
        external_task_key="acct:board:item",
        query="roof",
        k=20,
    )

    assert batch_calls == [
        {
            "queries": ["roof"],
            "k": 8,
            "max_evidence_chunks": 8,
        }
    ]
    assert results == [
        {
            "filename": "source.pdf",
            "page": 2,
            "section": "roof",
            "snippet": "Roof details",
            "score": 0.1,
            "fileId": "file-1",
            "mondayAssetId": "asset-1",
        }
    ]
