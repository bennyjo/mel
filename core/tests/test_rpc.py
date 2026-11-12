"""`core/rpc.py`: the usage journal records an endpoint label, never the URL.
Offline."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core import rpc


class TestEndpointLabel(unittest.TestCase):

    def test_default_endpoint_is_public(self):
        with mock.patch.dict(os.environ, {"MEL_RPC_URL": ""}):
            self.assertEqual(rpc.endpoint_label(), "public")

    def test_override_is_custom_and_url_not_recorded(self):
        url = "https://rpc.example.com/v1/SECRET-KEY"
        with mock.patch.dict(os.environ, {"MEL_RPC_URL": url}):
            self.assertEqual(rpc.endpoint_label(), "custom")

    def test_override_equal_to_default_is_public(self):
        with mock.patch.dict(os.environ, {"MEL_RPC_URL": rpc.protected_config()["rpc_url"]}):
            self.assertEqual(rpc.endpoint_label(), "public")

    def test_usage_row_has_label_not_url(self):
        import json
        import tempfile
        url = "https://rpc.example.com/v1/SECRET-KEY"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "rpc-usage.jsonl"
            with mock.patch.dict(os.environ, {"MEL_RPC_URL": url}), \
                    mock.patch.object(rpc, "USAGE_LOG", log), \
                    mock.patch.object(rpc.STATS, "http_requests", 1):
                rpc._append_usage()
            text = log.read_text()
        self.assertNotIn("SECRET-KEY", text)
        self.assertEqual(json.loads(text)["endpoint"], "custom")


if __name__ == "__main__":
    unittest.main()
