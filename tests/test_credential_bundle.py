import json
import sys
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))

import credential_bundle  # noqa: E402


class TestCredentialBundle(unittest.TestCase):
    def test_round_trip_preserves_accounts_and_tokens(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, target, bundle = root / "source", root / "target", root / "mail.mbvault"
            (source / "tokens").mkdir(parents=True)
            (source / "accounts.json").write_text('{"accounts": []}', encoding="utf-8")
            (source / "tokens" / "outlook.json").write_text('{"refresh_token": "secret"}', encoding="utf-8")

            credential_bundle.export_bundle(source, bundle, "correct horse battery staple")
            credential_bundle.import_bundle(bundle, target, "correct horse battery staple")

            self.assertEqual((target / "accounts.json").read_text(), '{"accounts": []}')
            self.assertEqual(json.loads((target / "tokens" / "outlook.json").read_text())["refresh_token"], "secret")
            self.assertNotIn("refresh_token", bundle.read_text(encoding="utf-8"))

    def test_wrong_password_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, bundle = root / "source", root / "mail.mbvault"
            source.mkdir()
            (source / "accounts.json").write_text('{"accounts": []}', encoding="utf-8")
            credential_bundle.export_bundle(source, bundle, "correct horse battery staple")
            with self.assertRaises(SystemExit):
                credential_bundle.import_bundle(bundle, root / "target", "this password is wrong")


if __name__ == "__main__":
    unittest.main()
