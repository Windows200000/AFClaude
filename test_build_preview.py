#!/usr/bin/env python3
"""Offline tests for tools/build_preview.py (docs/phase6a_plan.md §3.9, D-211): the hash-gated
rebuild check, the atomic publish into export_quickview.OUT, and main()'s preview_publish_enabled
gate -- none of these need the pinned Node or a real npm build (build() itself is exercised by
test_dashboard_web.py's test_preview_build; here build() is mocked)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir)
import export_quickview as qv  # noqa: E402
import tools.build_preview as bp  # noqa: E402


def enable():
    testenv.set_setting("preview_publish_enabled", True)


def disable():
    testenv.set_setting("preview_publish_enabled", None)


def write_fake_dist(root, js="console.log(1)", css="body{}"):
    os.makedirs(os.path.join(root, "assets"), exist_ok=True)
    with open(os.path.join(root, "index.html"), "w") as fh:
        fh.write("<!doctype html><meta http-equiv=Content-Security-Policy "
                 "content=\"default-src 'self'; connect-src 'none'\">")
    with open(os.path.join(root, "assets", "app-abc.js"), "w") as fh:
        fh.write(js)
    with open(os.path.join(root, "assets", "app-abc.css"), "w") as fh:
        fh.write(css)


class SourceAndTreeHash(unittest.TestCase):
    def test_source_hash_changes_with_content_not_just_mtime(self):
        with tempfile.TemporaryDirectory() as web:
            src = os.path.join(web, "src")
            os.makedirs(src)
            with open(os.path.join(web, "package.json"), "w") as fh:
                fh.write("{}")
            with open(os.path.join(web, "package-lock.json"), "w") as fh:
                fh.write("{}")
            with open(os.path.join(src, "a.ts"), "w") as fh:
                fh.write("export const a = 1;")
            old = (bp.WEB, bp.SOURCE_FILES, bp.SOURCE_DIRS)
            try:
                bp.WEB = web
                bp.SOURCE_FILES = (os.path.join(web, "package.json"), os.path.join(web, "package-lock.json"))
                bp.SOURCE_DIRS = (src,)
                h1 = bp.source_hash()
                self.assertEqual(h1, bp.source_hash())   # stable
                with open(os.path.join(src, "a.ts"), "w") as fh:
                    fh.write("export const a = 2;")
                self.assertNotEqual(h1, bp.source_hash())
            finally:
                bp.WEB, bp.SOURCE_FILES, bp.SOURCE_DIRS = old

    def test_tree_hash_ignores_name_order_not_content(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            write_fake_dist(d1)
            write_fake_dist(d2)
            self.assertEqual(bp.tree_hash(d1), bp.tree_hash(d2))
            write_fake_dist(d2, js="console.log(2)")
            self.assertNotEqual(bp.tree_hash(d1), bp.tree_hash(d2))


class ValidateDist(unittest.TestCase):
    def test_rejects_missing_index_connect_src_or_assets(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(bp.BuildError):
                bp.validate_dist(d)
            with open(os.path.join(d, "index.html"), "w") as fh:
                fh.write("<html></html>")   # no connect-src 'none'
            with self.assertRaises(bp.BuildError):
                bp.validate_dist(d)
            write_fake_dist(d)
            bp.validate_dist(d)   # now passes
            os.remove(os.path.join(d, "assets", "app-abc.css"))
            with self.assertRaises(bp.BuildError):
                bp.validate_dist(d)


class PublishAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_out = qv.OUT
        self.addCleanup(lambda: setattr(qv, "OUT", self.old_out))
        qv.OUT = os.path.join(self.tmp.name, "quickview")
        self.src = os.path.join(self.tmp.name, "dist-preview")
        write_fake_dist(self.src)
        self.old_dist = bp.DIST_PREVIEW
        bp.DIST_PREVIEW = self.src
        self.addCleanup(lambda: setattr(bp, "DIST_PREVIEW", self.old_dist))

    def test_publish_writes_meta_and_assets(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        bp.publish("v1", "srchash1", now)
        dest = os.path.join(qv.OUT, qv.PREVIEW_SUBDIR)
        self.assertTrue(os.path.isfile(os.path.join(dest, "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "assets", "app-abc.js")))
        with open(os.path.join(dest, bp.META_NAME)) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["version"], "v1")
        self.assertEqual(meta["source_hash"], "srchash1")
        self.assertIn("built_at_berlin", meta)

    def test_republish_replaces_old_content_leaves_no_tmp_or_old_dir(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        bp.publish("v1", "srchash1", now)
        write_fake_dist(self.src, js="console.log('v2')")
        bp.publish("v2", "srchash2", now)
        dest = os.path.join(qv.OUT, qv.PREVIEW_SUBDIR)
        with open(os.path.join(dest, "assets", "app-abc.js")) as fh:
            self.assertIn("v2", fh.read())
        leftovers = [f for f in os.listdir(qv.OUT) if f != qv.PREVIEW_SUBDIR]
        self.assertEqual(leftovers, [])


class PreviewPublishEnabledGate(unittest.TestCase):
    def setUp(self):
        self.addCleanup(disable)
        disable()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_out = qv.OUT
        self.addCleanup(lambda: setattr(qv, "OUT", self.old_out))
        qv.OUT = os.path.join(self.tmp.name, "quickview")
        self.src = os.path.join(self.tmp.name, "dist-preview")
        write_fake_dist(self.src)
        self.old_dist = bp.DIST_PREVIEW
        bp.DIST_PREVIEW = self.src
        self.addCleanup(lambda: setattr(bp, "DIST_PREVIEW", self.old_dist))

    def test_off_by_default_does_nothing(self):
        with mock.patch.object(bp, "build") as build:
            rc = bp.main([])
        build.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.isdir(qv.OUT))
        self.assertEqual(qv.preview_block(), {"enabled": False})

    def test_enabled_builds_once_then_skips_unchanged_then_rebuilds_on_force(self):
        enable()
        with mock.patch.object(bp, "build") as build, mock.patch.object(bp, "post_notice") as notice:
            self.assertEqual(bp.main([]), 0)
            build.assert_called_once()
            notice.assert_called_once()
            # unchanged sources: no rebuild, no second notice
            self.assertEqual(bp.main([]), 0)
            build.assert_called_once()
            notice.assert_called_once()
            # --force rebuilds even though nothing changed
            self.assertEqual(bp.main(["--force"]), 0)
            self.assertEqual(build.call_count, 2)

    def test_preview_block_reports_the_published_version(self):
        enable()
        with mock.patch.object(bp, "build"):
            bp.main([])
        block = qv.preview_block()
        self.assertEqual(block["enabled"], True)
        self.assertTrue(block["available"])
        self.assertEqual(block["url_path"], f"/private/{qv.PREVIEW_SUBDIR}/")
        self.assertIn("version", block)


if __name__ == "__main__":
    unittest.main()
