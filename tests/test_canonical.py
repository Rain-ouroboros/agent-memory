"""Synthetic seam tests; no host files, network or model providers."""
import unittest
from agent_memory.canonical import CanonicalReadAdapter, SnapshotError


def adapter(rows, authorize=None):
    return CanonicalReadAdapter(
        latest_rows=lambda: rows,
        metadata=lambda row: {
            "access_scope": row.get("scope"),
            "author": None,
            "source_ids": row.get("parents", []),
        },
        revision=lambda row: row.get("revision", ""),
        authorize=authorize or (
            lambda row, meta, audience, rev:
            meta["access_scope"] == audience and rev == "r1"
        ),
        eligible=lambda row: row.get("active") is True,
    )


def record(rid="synthetic:1", scope="private:test"):
    return {
        "id": rid, "text": "Synthetic observation", "revision": "r1",
        "scope": scope, "active": True, "parents": ["synthetic:parent"],
    }


class AdapterTests(unittest.TestCase):
    def test_returns_copies_preserving_unknown_author_and_parents(self):
        rows = [record()]
        result = adapter(rows).select(audience="private:test")
        item = result.records[0]
        self.assertIsNone(item["metadata"]["author"])
        self.assertEqual(item["metadata"]["source_ids"], ["synthetic:parent"])
        item["record"]["parents"].append("changed")
        self.assertEqual(rows[0]["parents"], ["synthetic:parent"])
        self.assertFalse(result.actual_write_performed)

    def test_private_unknown_and_inactive_withheld(self):
        rows = [record(scope="private:other"), record("b", "unknown"),
                record("c", None), {**record("d"), "active": False}]
        result = adapter(rows).select(audience="private:test")
        self.assertEqual(result.records, ())
        self.assertEqual(result.coverage, "authorized_snapshot_selection")

    def test_duplicate_revisions_are_not_silently_folded(self):
        with self.assertRaises(SnapshotError):
            adapter([record(), record()]).select(audience="private:test")

    def test_missing_revision_is_error(self):
        with self.assertRaises(SnapshotError):
            adapter([{**record(), "revision": ""}]).select(
                audience="private:test")

    def test_truthy_nonboolean_authorization_denies(self):
        result = adapter([record()], lambda *args: "yes").select(
            audience="private:test")
        self.assertEqual(result.records, ())

    def test_limit_and_truncation(self):
        result = adapter([record("a"), record("b")]).select(
            audience="private:test", limit=1)
        self.assertEqual(len(result.records), 1)
        self.assertTrue(result.truncated)

    def test_callback_error_propagates_not_empty_success(self):
        def broken(*args):
            raise RuntimeError("synthetic policy unavailable")
        with self.assertRaises(RuntimeError):
            adapter([record()], broken).select(audience="private:test")


if __name__ == "__main__":
    unittest.main()
