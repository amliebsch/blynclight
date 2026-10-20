import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blynclight_teams.config import Config, load_config, save_config  # noqa: E402
from blynclight_teams.errors import ConfigError, NotSignedIn  # noqa: E402
from blynclight_teams.graph import GraphClient, Token, resolve_tenant, tenant_segment  # noqa: E402


def _config(directory: Path) -> Config:
    return Config(
        client_id="app-id",
        tenant="organizations",
        directory=directory,
        status_directory=directory,
    )


class GraphTests(unittest.TestCase):
    def test_login_waits_through_authorization_pending(self) -> None:
        clock = {"t": 0.0}
        polls = {"n": 0}

        def http(method, url, headers, body):
            if url.endswith("/devicecode"):
                self.assertIn(b"client_id=app-id", body)
                return 200, json.dumps(
                    {
                        "device_code": "dc",
                        "user_code": "ABCD-EFGH",
                        "verification_uri": "https://microsoft.com/devicelogin",
                        "expires_in": 900,
                        "interval": 5,
                        "message": "Enter the code",
                    }
                ).encode()
            if url.endswith("/token"):
                polls["n"] += 1
                if polls["n"] == 1:
                    return 400, json.dumps({"error": "authorization_pending"}).encode()
                return 200, json.dumps(
                    {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
                ).encode()
            if url.endswith("/me"):
                self.assertEqual(headers["Authorization"], "Bearer tok")
                return 200, json.dumps(
                    {"displayName": "Ada Lovelace", "mail": "ada@contoso.com"}
                ).encode()
            if url.endswith("/me/presence"):
                return 200, json.dumps(
                    {"availability": "Available", "activity": "Available"}
                ).encode()
            self.fail(url)

        def sleep(seconds: float) -> None:
            clock["t"] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            client = GraphClient(_config(Path(tmp)), http=http, now=lambda: clock["t"])
            with contextlib.redirect_stdout(io.StringIO()):
                token, presence, account = client.login(sleep=sleep)

        self.assertEqual(token.access_token, "tok")
        self.assertEqual(presence.label, "Available/Available")
        self.assertEqual(account, "Ada Lovelace <ada@contoso.com>")
        self.assertEqual(polls["n"], 2)

    def test_expired_token_is_refreshed_before_presence(self) -> None:
        def http(method, url, headers, body):
            if url.endswith("/token"):
                self.assertIn(b"grant_type=refresh_token", body)
                self.assertIn(b"refresh_token=refresh-me", body)
                return 200, json.dumps(
                    {"access_token": "fresh", "refresh_token": "next", "expires_in": 3600}
                ).encode()
            if url.endswith("/me/presence"):
                self.assertEqual(headers["Authorization"], "Bearer fresh")
                return 200, json.dumps({"availability": "Busy", "activity": "InACall"}).encode()
            self.fail(url)

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            config.token_path.write_text(
                json.dumps(Token("stale", "refresh-me", expires_at=0).to_json())
            )
            client = GraphClient(config, http=http, now=lambda: 10_000)
            presence = client.get_presence()
            saved = json.loads(config.token_path.read_text())
            mode = stat.S_IMODE(config.token_path.stat().st_mode)

        self.assertEqual(presence.activity, "InACall")
        self.assertEqual(saved["access_token"], "fresh")
        self.assertEqual(mode, 0o600)

    def test_presence_forbidden_tells_the_user_to_grant_consent(self) -> None:
        def http(method, url, headers, body):
            return 403, b'{"error":{"code":"Authorization_RequestDenied"}}'

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            config.token_path.write_text(
                json.dumps(Token("tok", "ref", expires_at=10**12).to_json())
            )
            client = GraphClient(config, http=http, now=lambda: 0)
            with self.assertRaises(NotSignedIn) as caught:
                client.get_presence()
        self.assertIn("Presence.Read", str(caught.exception))

    def test_config_round_trip_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            save_config(Config("{app-id}", "contoso.onmicrosoft.com", 15, directory))
            loaded = load_config(directory)
            self.assertEqual(loaded.client_id, "app-id")
            self.assertEqual(loaded.tenant, "contoso.onmicrosoft.com")
            self.assertEqual(loaded.poll_seconds, 15)
            (directory / "config.toml").write_text('client_id = "x"\npoll_seconds = 1\n')
            with self.assertRaises(ConfigError):
                load_config(directory)


class TenantResolutionTests(unittest.TestCase):
    def test_upn_uses_the_domain_after_at(self) -> None:
        self.assertEqual(tenant_segment("ada@contoso.com"), "contoso.com")
        self.assertEqual(
            tenant_segment("ada_contoso.com#EXT#@fabrikam.onmicrosoft.com"),
            "fabrikam.onmicrosoft.com",
        )

    def test_guid_and_aliases_pass_through(self) -> None:
        guid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.assertEqual(resolve_tenant(guid, http=self.fail_http), guid)
        self.assertEqual(resolve_tenant("organizations", http=self.fail_http), "organizations")

    def test_domain_lookup_stores_the_issuer_tenant(self) -> None:
        guid = "11111111-2222-3333-4444-555555555555"

        def http(method, url, headers, body):
            self.assertEqual(method, "GET")
            self.assertIn("/contoso.com/v2.0/.well-known/openid-configuration", url)
            return 200, json.dumps(
                {"issuer": f"https://login.microsoftonline.com/{guid}/v2.0"}
            ).encode()

        self.assertEqual(resolve_tenant("ada@contoso.com", http=http), guid)

    def test_missing_directory_is_reported(self) -> None:
        def http(method, url, headers, body):
            return 400, json.dumps(
                {"error": "invalid_tenant", "error_description": "not found"}
            ).encode()

        with self.assertRaises(NotSignedIn) as caught:
            resolve_tenant("missing.example", http=http)
        self.assertIn("missing.example", str(caught.exception))

    def test_aadsts50059_asks_for_upn_or_tenant(self) -> None:
        def http(method, url, headers, body):
            return 400, json.dumps(
                {
                    "error": "invalid_request",
                    "error_description": "AADSTS50059: No tenant-identifying information found.",
                }
            ).encode()

        with tempfile.TemporaryDirectory() as tmp:
            client = GraphClient(_config(Path(tmp)), http=http)
            with self.assertRaises(NotSignedIn) as caught:
                client.request_device_code()
        self.assertIn("--upn", str(caught.exception))
        self.assertIn("--tenant", str(caught.exception))

    def test_malformed_upn_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            tenant_segment("not-an-email")

    @staticmethod
    def fail_http(method, url, headers, body):
        raise AssertionError(url)


if __name__ == "__main__":
    unittest.main()
