from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from nighthawk.__main__ import main
from nighthawk.config import ConfigurationError, load_platform
from nighthawk.grafana import datasource_uid, desired_state, reconcile, secret_reader
from tests.fakes import add_stream, example_document, materialize_plain, secret_for, write_document


class FakeGrafana:
    """In-memory organizations and data sources behind the admin API calls the reconciler makes."""

    def __init__(self, status: int | None = None) -> None:
        self.orgs: dict[int, str] = {1: "Main Org."}
        self.datasources: dict[int, dict[str, dict]] = {1: {}}
        self.passwords: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.status = status

    def __call__(self, method: str, path: str, body: object = None, org_id: int | None = None):
        self.calls.append((method, path))
        if self.status is not None:
            return self.status, {"message": "Invalid username or password"}
        if (method, path) == ("GET", "/api/orgs"):
            return 200, [{"id": key, "name": name} for key, name in self.orgs.items()]
        if method == "GET" and path.startswith("/api/orgs/name/"):
            name = path.rsplit("/", 1)[1]
            found = [key for key, value in self.orgs.items() if value == name]
            return (200, {"id": found[0], "name": name}) if found else (404, {"message": "Organization not found"})
        if (method, path) == ("POST", "/api/orgs"):
            org = max(self.orgs) + 1
            self.orgs[org], self.datasources[org] = body["name"], {}
            return 200, {"orgId": org, "message": "Organization created"}
        store = self.datasources[org_id]
        if (method, path) == ("GET", "/api/datasources"):
            # Like Grafana 13.2.3, the list leaves out basicAuthUser.
            return 200, [{key: value for key, value in item.items() if key != "basicAuthUser"} for item in store.values()]
        if (method, path) == ("POST", "/api/datasources"):
            return self._save(store, body)
        uid = path.rsplit("/", 1)[1]
        if method == "GET":
            return (200, dict(store[uid])) if uid in store else (404, {"message": "Data source not found"})
        if method == "PUT":
            return self._save(store, body)
        if method == "DELETE":
            del store[uid]
            return 200, {"message": "Data source deleted"}
        raise AssertionError(f"unexpected call {method} {path}")

    def _save(self, store: dict, body: dict):
        public = {key: value for key, value in body.items() if key != "secureJsonData"}
        public["secureJsonFields"] = {"basicAuthPassword": True}
        store[body["uid"]] = public
        self.passwords[body["uid"]] = body["secureJsonData"]["basicAuthPassword"]
        return 200, {"message": "ok"}

    def modifying_calls(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]


class GrafanaFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_document()
        add_stream(self.data, "example", "infrastructure", "example-infrastructure")
        add_stream(self.data, "second", "application", "second-application")
        for signal in ("traces", "profiles"):
            del self.data["tenants"][1]["datastreams"][0]["signals"][signal]

    def platform(self):
        return load_platform(write_document(self.root, self.data))

    def state(self) -> dict:
        return desired_state(self.platform())


class DesiredStateTests(GrafanaFixture):
    def test_one_organization_per_tenant_and_one_data_source_per_enabled_signal(self) -> None:
        state = self.state()
        self.assertEqual(
            [(item["name"], len(item["datasources"])) for item in state["organizations"]],
            [("example", 8), ("second", 2)],
        )
        self.assertEqual(state["settings"], {"auth.anonymous": {"enabled": False}})

    def test_render_is_deterministic(self) -> None:
        first = json.dumps(self.state(), sort_keys=True)
        self.data["tenants"].reverse()
        self.data["credentials"].reverse()
        self.assertEqual(first, json.dumps(self.state(), sort_keys=True))

    def test_uids_are_stable_valid_and_unique(self) -> None:
        before = {item["uid"] for org in self.state()["organizations"] for item in org["datasources"]}
        add_stream(self.data, "third", "application", "t" * 63)
        after = [item["uid"] for org in self.state()["organizations"] for item in org["datasources"]]
        self.assertLessEqual(before, set(after))
        self.assertEqual(len(after), len(set(after)))
        for uid in after:
            self.assertRegex(uid, r"^[a-zA-Z0-9_-]{1,40}$")
        self.assertEqual(datasource_uid("t" * 63, "metrics"), datasource_uid("t" * 63, "metrics"))

    def test_data_sources_reach_only_the_gateway_with_their_own_query_credential(self) -> None:
        platform = self.platform()
        text = json.dumps(self.state())
        self.assertNotIn("X-Scope-OrgID", text)
        for address in platform.gateway.upstreams.values():
            self.assertNotIn(address.split("://", 1)[1], text)
        for organization in self.state()["organizations"]:
            for item in organization["datasources"]:
                self.assertTrue(item["url"].startswith("https://gateway.nighthawk.internal:443/"))
                credential = next(entry for entry in platform.credentials if entry.id == item["basicAuthUser"])
                self.assertEqual((credential.tenant, credential.permission), (organization["name"], "query"))
                self.assertEqual(item["secret_ref"], credential.secret_ref)
                self.assertEqual(item["uid"], datasource_uid(credential.backend_id, item["name"].split()[1]))
                self.assertNotIn("password", json.dumps(item).lower())

    def test_correlations_stay_inside_one_datastream(self) -> None:
        organizations = {item["name"]: item["datasources"] for item in self.state()["organizations"]}

        def source(tenant: str, backend_id: str, signal: str) -> dict:
            return next(item for item in organizations[tenant] if item["uid"] == datasource_uid(backend_id, signal))

        traces = source("example", "example-application", "traces")["jsonData"]
        self.assertEqual(traces["tracesToLogsV2"]["datasourceUid"], datasource_uid("example-application", "logs"))
        self.assertEqual(traces["tracesToMetrics"]["datasourceUid"], datasource_uid("example-application", "metrics"))
        self.assertEqual(traces["tracesToProfiles"]["datasourceUid"], datasource_uid("example-application", "profiles"))
        trace_uid = datasource_uid("example-application", "traces")
        self.assertEqual(source("example", "example-application", "logs")["jsonData"]["derivedFields"][0]["datasourceUid"], trace_uid)
        self.assertEqual(
            source("example", "example-application", "metrics")["jsonData"]["exemplarTraceIdDestinations"][0]["datasourceUid"],
            trace_uid,
        )
        # No traces in this datastream, so nothing links to a trace data source.
        self.assertEqual(source("second", "second-application", "logs")["jsonData"], {})
        self.assertNotIn("exemplarTraceIdDestinations", source("second", "second-application", "metrics")["jsonData"])
        for tenant, datasources in organizations.items():
            own = {item["uid"] for item in datasources}
            for item in datasources:
                by_backend = {other["uid"] for other in datasources if other["uid"][5:] == item["uid"][5:]}
                for target in re.findall(r'"datasourceUid": "([^"]+)"', json.dumps(item["jsonData"])):
                    self.assertIn(target, own & by_backend)

    def test_traces_without_profiles_have_no_profile_link(self) -> None:
        del self.data["tenants"][0]["datastreams"][0]["signals"]["profiles"]
        traces = next(
            item for item in self.state()["organizations"][0]["datasources"]
            if item["uid"] == datasource_uid("example-application", "traces")
        )
        self.assertNotIn("tracesToProfiles", traces["jsonData"])


class ReconcileTests(GrafanaFixture):
    def setUp(self) -> None:
        super().setUp()
        self.grafana = FakeGrafana()
        platform = self.platform()
        self.read = secret_reader(platform, materialize_plain(platform, self.root / "materialized"))

    def test_first_apply_creates_everything_and_second_apply_changes_nothing(self) -> None:
        first = reconcile(self.state(), self.grafana, self.read)
        self.assertEqual(len(first.created), 2 + 10)
        self.assertEqual(sorted(self.grafana.orgs.values()), ["Main Org.", "example", "second"])
        uid = datasource_uid("second-application", "logs")
        self.assertEqual(self.grafana.passwords[uid], secret_for("second-application-query"))
        self.grafana.calls.clear()
        second = reconcile(self.state(), self.grafana, self.read)
        self.assertFalse(second.changed)
        self.assertEqual(second.lines(False), ["no changes"])
        self.assertEqual(self.grafana.modifying_calls(), [])

    def test_changed_data_source_is_updated_in_place(self) -> None:
        reconcile(self.state(), self.grafana, self.read)
        self.data["gateway"]["hostname"] = "gateway.example.net"
        changes = reconcile(self.state(), self.grafana, self.read)
        self.assertEqual((len(changes.created), len(changes.updated)), (0, 10))
        self.assertTrue(all(call[0] == "PUT" for call in self.grafana.modifying_calls()[-10:]))

    def test_update_secrets_resends_passwords(self) -> None:
        reconcile(self.state(), self.grafana, self.read)
        changes = reconcile(self.state(), self.grafana, self.read, update_secrets=True)
        self.assertEqual(len(changes.updated), 10)

    def test_undeclared_items_are_reported_and_pruned_only_on_request(self) -> None:
        reconcile(self.state(), self.grafana, self.read)
        self.grafana.datasources[2]["manual"] = {"uid": "manual", "name": "hand made"}
        del self.data["tenants"][0]["datastreams"][1]
        self.data["credentials"] = [item for item in self.data["credentials"] if item["datastream"] != "infrastructure"]
        self.data["tenants"] = [item for item in self.data["tenants"] if item["id"] != "second"]
        self.data["credentials"] = [item for item in self.data["credentials"] if item["tenant"] != "second"]
        self.grafana.calls.clear()
        changes = reconcile(self.state(), self.grafana, self.read)
        self.assertIn("organization second", changes.undeclared)
        self.assertEqual(len(changes.undeclared), 1 + 4)
        self.assertEqual(self.grafana.modifying_calls(), [])
        changes = reconcile(self.state(), self.grafana, self.read, prune=True)
        self.assertEqual(len(changes.deleted), 4)
        # Organizations and data sources NightHawk did not create are never removed.
        self.assertIn("second", self.grafana.orgs.values())
        self.assertIn("manual", self.grafana.datasources[2])
        self.assertEqual(len(self.grafana.datasources[3]), 2)

    def test_dry_run_sends_no_modifying_request_and_needs_no_secret(self) -> None:
        def unreadable(secret_ref: str) -> str:
            raise AssertionError("a dry run must not read secrets")

        changes = reconcile(self.state(), self.grafana, unreadable, dry_run=True)
        self.assertEqual(len(changes.created), 12)
        self.assertEqual(self.grafana.modifying_calls(), [])
        self.assertTrue(all(line.startswith("would create") for line in changes.lines(True)))

    def test_unauthorized_grafana_fails_naming_the_request_only(self) -> None:
        with self.assertRaises(ConfigurationError) as raised:
            reconcile(self.state(), FakeGrafana(status=401), self.read)
        self.assertEqual(str(raised.exception), "GET /api/orgs: Grafana answered HTTP 401")

    def test_cli_reports_an_unreachable_grafana_without_the_password(self) -> None:
        password = self.root / "admin-password"
        password.write_text("admin-secret-value\n", encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors), redirect_stdout(io.StringIO()):
            result = main([
                "provision-grafana", "--config", str(write_document(self.root, self.data)),
                "--url", "http://127.0.0.1:9", "--admin-user", "admin",
                "--admin-password-file", str(password), "--dry-run",
            ])
        self.assertEqual(result, 1)
        self.assertIn("GET /api/orgs: cannot reach Grafana", errors.getvalue())
        self.assertNotIn("admin-secret-value", errors.getvalue())

    def test_cli_dry_run_against_the_fake(self) -> None:
        password = self.root / "admin-password"
        password.write_text("admin-secret-value\n", encoding="utf-8")
        output = io.StringIO()
        with mock.patch("nighthawk.__main__.grafana.http_client", return_value=self.grafana), redirect_stdout(output):
            self.assertEqual(main([
                "provision-grafana", "--config", str(write_document(self.root, self.data)),
                "--url", "http://grafana.invalid", "--admin-user", "admin",
                "--admin-password-file", str(password), "--dry-run",
            ]), 0)
        self.assertIn("would create organization example", output.getvalue())
        self.assertEqual(self.grafana.modifying_calls(), [])


if __name__ == "__main__":
    unittest.main()
