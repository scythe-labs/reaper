# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon label writes batch their reads.

A shelf reconcile after a list was changed from another tool applies hundreds of label
edits, and the old path fetched every item individually (``add_label``) or fetched and
``reload()``-ed every item and then saved one item at a time (``remove_label``) -- ~700
serial Plex round-trips on a 341-change pass, the dominant cost of that pass. The reads
now batch: ONE ``/library/metadata/<id,id,...>`` GET per chunk (``fetchItems`` and
``fetchItem`` hit the same endpoint), and one edit per label spelling per chunk. A fake
section here COUNTS every read and edit, so a regression back toward per-item fetches
fails loudly.

The label-*preserve* property (adding "Leaving Soon" must not wipe an item's other labels)
is server-side -- Plex's batch endpoint is additive -- and is unchanged by batching the
read; it is verified against a live server, not here.
"""

from __future__ import annotations

from reaper.clients.plex import BATCH_SIZE, PlexClient
from reaper.config import RuntimeSafety


class _Tag:
    def __init__(self, tag: str) -> None:
        self.tag = tag


class _Item:
    def __init__(
        self, rating_key: int, labels: list[str], collections: list[str] | None = None
    ) -> None:
        self.ratingKey = rating_key
        self.labels = [_Tag(t) for t in labels]
        self.collections = [_Tag(t) for t in (collections or [])]


class _Edit:
    """Records one ``batchMultiEdits(...).<op>(tag).saveMultiEdits()`` chain."""

    def __init__(self, server: _FakeLabelServer, items: list[_Item]) -> None:
        self._server = server
        self._items = items
        self._op: tuple[str, str] | None = None

    def addLabel(self, label: str) -> _Edit:  # noqa: N802 - mirrors plexapi
        self._op = ("add", label)
        return self

    def removeLabel(self, label: str) -> _Edit:  # noqa: N802 - mirrors plexapi
        self._op = ("remove", label)
        return self

    def removeCollection(self, name: str, locked: bool = True) -> _Edit:  # noqa: N802 - plexapi
        # plexapi locks the collection field on removal; the caller passes locked=True
        # explicitly (a deliberate, documented delta from the old per-child DELETE).
        assert locked is True
        self._op = ("remove_collection", name)
        return self

    def saveMultiEdits(self) -> _Edit:  # noqa: N802 - mirrors plexapi
        assert self._op is not None, "an edit must name its op before saving"
        op, tag = self._op
        self._server.edits.append((op, tag, [i.ratingKey for i in self._items]))
        return self


class _FakeSection:
    def __init__(self, server: _FakeLabelServer) -> None:
        self._server = server

    def fetchItems(self, keys: list[int]) -> list[_Item]:  # noqa: N802 - mirrors plexapi
        # A list of ids resolves to ONE /library/metadata/<ids> read. Items missing from
        # Plex (deleted since the scan) simply do not come back -- mirrored here by skipping
        # keys the server does not hold.
        self._server.fetches.append(list(keys))
        return [self._server.items[k] for k in keys if k in self._server.items]

    def batchMultiEdits(self, items: list[_Item]) -> _Edit:  # noqa: N802 - mirrors plexapi
        return _Edit(self._server, items)


class _FakeLibrary:
    def __init__(self, server: _FakeLabelServer) -> None:
        self._server = server

    def section(self, title: str) -> _FakeSection:
        return _FakeSection(self._server)

    def sectionByID(self, section_id: int) -> _FakeSection:  # noqa: N802 - mirrors plexapi
        # Addressed by key, not title: two libraries can share a title, so the collection
        # detach resolves the section by its stable id.
        self._server.section_ids.append(section_id)
        return _FakeSection(self._server)


class _FakeLabelServer:
    def __init__(self, items: dict[int, _Item]) -> None:
        self.items = items
        self.library = _FakeLibrary(self)
        self.fetches: list[list[int]] = []
        self.edits: list[tuple[str, str, list[int]]] = []
        self.section_ids: list[int] = []


def _client(server: _FakeLabelServer) -> PlexClient:
    client = PlexClient("http://plex.local", "token", safety=RuntimeSafety())
    client._server = server  # type: ignore[assignment]
    return client


class TestAddLabelBatchesReads:
    async def test_one_multi_id_read_and_one_edit_per_chunk(self) -> None:
        keys = list(range(1, 251))  # 250 items -> chunks of 100, 100, 50
        server = _FakeLabelServer({k: _Item(k, ["Favorite"]) for k in keys})
        await _client(server).add_label(7, keys, "Leaving Soon")

        # The section is resolved by key, never by title (rule 57): two libraries can share a
        # title and the title lookup returns only the last match.
        assert server.section_ids == [7]
        # Reads are batched: three multi-id GETs (one per chunk), never one per item.
        assert [len(f) for f in server.fetches] == [BATCH_SIZE, BATCH_SIZE, 50]
        # One additive label edit per chunk, over exactly that chunk's items.
        assert [(op, tag) for op, tag, _ in server.edits] == [("add", "Leaving Soon")] * 3
        assert server.edits[0][2] == keys[:BATCH_SIZE]

    async def test_it_skips_items_the_read_no_longer_returns(self) -> None:
        # 2 was deleted from Plex after the scan; the read returns 1 and 3 only.
        server = _FakeLabelServer({1: _Item(1, []), 3: _Item(3, [])})
        await _client(server).add_label(7, [1, 2, 3], "Leaving Soon")
        assert server.fetches == [[1, 2, 3]]
        # The edit covers only the items that came back -- a since-deleted item is skipped,
        # never a failed reconcile.
        assert server.edits == [("add", "Leaving Soon", [1, 3])]

    async def test_no_keys_touches_plex_at_all(self) -> None:
        server = _FakeLabelServer({})
        await _client(server).add_label(7, [], "Leaving Soon")
        assert server.fetches == [] and server.edits == []


class TestRemoveLabelBatchesReads:
    async def test_only_labeled_items_edited_under_their_stored_spelling(self) -> None:
        server = _FakeLabelServer(
            {
                1: _Item(1, ["Leaving Soon", "Favorite"]),  # carries it (canonical case)
                2: _Item(2, ["Favorite"]),  # does NOT carry it
                3: _Item(3, ["leaving soon"]),  # carries a stale lower-cased spelling
            }
        )
        await _client(server).remove_label(7, [1, 2, 3], "Leaving Soon")

        # The section is resolved by key, never by title (rule 57).
        assert server.section_ids == [7]
        # One multi-id read for the chunk, never one fetch + reload per item.
        assert server.fetches == [[1, 2, 3]]
        # Item 2 is never edited. Removal targets each item's STORED spelling (case-correct
        # against Plex's title-casing), so the two spellings become two writes.
        assert all(op == "remove" for op, _, _ in server.edits)
        assert {tag: keys for _, tag, keys in server.edits} == {
            "Leaving Soon": [1],
            "leaving soon": [3],
        }

    async def test_reads_batch_per_chunk(self) -> None:
        keys = list(range(1, 151))  # 150 items -> chunks of 100, 50
        server = _FakeLabelServer({k: _Item(k, ["Leaving Soon"]) for k in keys})
        await _client(server).remove_label(7, keys, "Leaving Soon")
        assert [len(f) for f in server.fetches] == [BATCH_SIZE, 50]
        # One remove edit per chunk (all one spelling), over that chunk's items.
        assert [(op, tag, len(k)) for op, tag, k in server.edits] == [
            ("remove", "Leaving Soon", BATCH_SIZE),
            ("remove", "Leaving Soon", 50),
        ]


class TestRemoveCollectionMembersBatchesReads:
    async def test_one_read_and_one_detach_per_chunk(self) -> None:
        keys = list(range(1, 151))  # 150 items -> chunks of 100, 50
        server = _FakeLabelServer({k: _Item(k, [], collections=["Leaving Soon"]) for k in keys})
        await _client(server).remove_collection_members(7, name="Leaving Soon", rating_keys=keys)
        # The section is resolved by key, never by title (once for the whole write).
        assert server.section_ids == [7]
        # One multi-id read per chunk, and one collection detach per chunk over that chunk's
        # items -- never one DELETE per item.
        assert [len(f) for f in server.fetches] == [BATCH_SIZE, 50]
        assert [(op, tag, len(k)) for op, tag, k in server.edits] == [
            ("remove_collection", "Leaving Soon", BATCH_SIZE),
            ("remove_collection", "Leaving Soon", 50),
        ]

    async def test_a_case_variant_collection_is_still_detached_under_its_stored_spelling(
        self,
    ) -> None:
        """find_collection adopts a case-variant shelf, so the detach must too: an item on a
        "leaving soon" collection is removed under that exact stored spelling, not the
        constant. A case-sensitive removal of the constant would detach nothing and the item
        would sit on the shelf forever (B-5)."""
        server = _FakeLabelServer(
            {
                1: _Item(1, [], collections=["Leaving Soon"]),  # canonical case
                2: _Item(2, [], collections=["Comedy"]),  # not on the shelf at all
                3: _Item(3, [], collections=["leaving soon"]),  # stale lower-cased spelling
            }
        )
        await _client(server).remove_collection_members(
            2, name="Leaving Soon", rating_keys=[1, 2, 3]
        )
        assert server.fetches == [[1, 2, 3]]
        # Item 2 is never edited; the two spellings become two detaches under their own text.
        assert all(op == "remove_collection" for op, _, _ in server.edits)
        assert {tag: keys for _, tag, keys in server.edits} == {
            "Leaving Soon": [1],
            "leaving soon": [3],
        }

    async def test_no_keys_touches_plex_at_all(self) -> None:
        server = _FakeLabelServer({})
        await _client(server).remove_collection_members(2, name="Leaving Soon", rating_keys=[])
        assert server.fetches == [] and server.edits == []


class _SessionStub:
    def delete(self) -> None: ...  # a marker plexapi would pass as the request method


class _FakeCollectionServer:
    """Records the raw queries delete_collection issues (no section/edit machinery)."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self._session = _SessionStub()

    def query(self, path: str, method: object = None) -> None:
        self.queries.append(path)


class TestDeleteCollection:
    async def test_it_deletes_the_whole_collection_in_one_request(self) -> None:
        server = _FakeCollectionServer()
        client = PlexClient("http://plex.local", "token", safety=RuntimeSafety())
        client._server = server  # type: ignore[assignment]
        await client.delete_collection(900)
        # One DELETE to the whole-collection path, never per-member /children/ deletes.
        assert server.queries == ["/library/collections/900"]
