from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

from nighthawk.config import ConfigurationError, ROOT, load_network, load_platform
from nighthawk.gateway import ROUTES, render_artifacts, traefik_dynamic, traefik_static
from tests.fakes import example_document, four_stream_document, write_document


class GatewayRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rules = load_network(ROOT / "config" / "network.yaml")
        self.data = four_stream_document()

    def platform(self):
        return load_platform(write_document(Path(self.temp.name), self.data))

    def test_every_router_is_authenticated_tls_only_and_host_bound(self) -> None:
        dynamic = traefik_dynamic(self.platform())
        routers, middlewares = dynamic["http"]["routers"], dynamic["http"]["middlewares"]
        self.assertEqual(len(routers), (len(ROUTES) + 1) * 2)
        backend_services = {route.upstream.replace(".", "-") for route in ROUTES}
        for name, router in routers.items():
            if router["service"] == "grafana-ui":
                continue
            self.assertIn(router["service"], backend_services)
            with self.subTest(router=name):
                (entry,) = router["entryPoints"]
                auth = [item for item in router["middlewares"] if "forwardAuth" in middlewares[item]]
                self.assertEqual(len(auth), 1)
                # The certificate must be attached before the auth call, and the credential removed after it.
                self.assertLess(router["middlewares"].index("client-certificate"), router["middlewares"].index(auth[0]))
                self.assertLess(router["middlewares"].index(auth[0]), router["middlewares"].index("strip-credentials"))
                self.assertEqual(router["tls"], {})
                self.assertTrue(router["rule"].startswith("Host(`gateway.nighthawk.internal`) && "))
                query = parse_qs(urlsplit(middlewares[auth[0]]["forwardAuth"]["address"]).query)
                route = next(item for item in ROUTES if name == f"{entry}-{item.name}")
                self.assertEqual(query, {"entry": [entry], "signal": [route.signal], "permission": [route.permission]})
                self.assertEqual(middlewares[auth[0]]["forwardAuth"]["authResponseHeaders"], ["X-Scope-OrgID"])
                self.assertIn(router["service"], dynamic["http"]["services"])

    def test_grafana_ui_is_routed_by_its_own_host_without_tenant_binding(self) -> None:
        dynamic = traefik_dynamic(self.platform())
        ui = {name: router for name, router in dynamic["http"]["routers"].items() if router["service"] == "grafana-ui"}
        self.assertEqual(sorted(ui), ["port-443-grafana-ui", "port-8443-grafana-ui"])
        for router in ui.values():
            self.assertEqual(router["rule"], "Host(`grafana.nighthawk.internal`)")
            self.assertEqual(router["tls"], {})
            self.assertNotIn("middlewares", router)
        self.assertEqual(
            dynamic["http"]["services"]["grafana-ui"]["loadBalancer"]["servers"], [{"url": "http://grafana:3000"}],
        )
        # No signal-backend router answers for the UI host name.
        for name, router in dynamic["http"]["routers"].items():
            if name not in ui:
                self.assertNotIn("grafana.nighthawk.internal", router["rule"])
                self.assertIn("Host(`gateway.nighthawk.internal`)", router["rule"])

    def test_there_is_no_catch_all_or_administrative_route(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route.name):
                self.assertNotRegex(route.match, r"PathPrefix\(`/`\)|HostRegexp|^Host")
                self.assertRegex(route.match, r"^(Path|PathPrefix|PathRegexp)\(`\^?/[a-z]")
        patterns = [re.compile(match) for route in ROUTES for match in re.findall(r"PathRegexp\(`(.*)`\)", route.match)]
        for path in (
            "/logs/loki/api/v1/push", "/logs/loki/api/v1/delete", "/logs/loki/api/v1/rules",
            "/metrics/prometheus/config/v1/rules", "/metrics/prometheus/api/v1/admin/tsdb/delete_series",
            "/metrics/api/v1/push", "/traces/api/overrides", "/metrics/prometheus/api/v1/query/../../../config",
        ):
            self.assertFalse(any(pattern.search(path) for pattern in patterns), path)
        self.assertTrue(any(pattern.search("/logs/loki/api/v1/label/app/values") for pattern in patterns))

    def test_ingest_and_query_routes_never_share_a_match(self) -> None:
        for signal in ("metrics", "logs", "traces", "profiles"):
            ingest = {route.match for route in ROUTES if (route.signal, route.permission) == (signal, "ingest")}
            query = {route.match for route in ROUTES if (route.signal, route.permission) == (signal, "query")}
            self.assertTrue(ingest and query)
            self.assertFalse(ingest & query)

    def test_entry_point_ports_are_the_selected_network_rules(self) -> None:
        static = traefik_static(self.platform(), self.rules)
        listeners = {name: entry for name, entry in static["entryPoints"].items() if name != "metrics"}
        self.assertEqual({entry["address"] for entry in listeners.values()}, {":443", ":8443"})
        for entry in listeners.values():
            self.assertEqual(entry["http"]["tls"], {})
            self.assertEqual(entry["http"]["aliasHeadersStrategy"], "delete")
        self.assertEqual(static["entryPoints"]["metrics"]["address"], ":8082")
        self.data["gateway"].update(entry_points=["remote-gateway"], grafana_entry_point="remote-gateway")
        static = traefik_static(self.platform(), self.rules)
        self.assertEqual(sorted(static["entryPoints"]), ["metrics", "port-443"])

    def test_routes_exist_only_for_signals_with_an_upstream(self) -> None:
        data = example_document()
        del data["tenants"][0]["datastreams"][0]["signals"]["profiles"]
        del data["gateway"]["upstreams"]["profiles"]
        self.data = data
        dynamic = traefik_dynamic(self.platform())
        self.assertFalse([name for name in dynamic["http"]["routers"] if "profiles" in name])
        self.assertNotIn("profiles", dynamic["http"]["services"])

    def test_client_certificates_are_verified_when_given(self) -> None:
        options = traefik_dynamic(self.platform())["tls"]["options"]["default"]
        self.assertEqual(options["clientAuth"]["clientAuthType"], "VerifyClientCertIfGiven")
        self.assertTrue(options["clientAuth"]["caFiles"])

    def test_artifacts_are_deterministic_and_secret_free(self) -> None:
        first = render_artifacts(self.platform(), self.rules)
        self.data["tenants"].reverse()
        self.data["credentials"].reverse()
        self.assertEqual(first, render_artifacts(self.platform(), self.rules))
        self.assertEqual(
            set(first), {"gateway/traefik-dynamic.yaml", "gateway/traefik-static.yaml", "gateway/routes.md"},
        )
        yaml.safe_load(first["gateway/traefik-dynamic.yaml"])
        for content in first.values():
            self.assertNotIn("example-ingest", content)
        self.assertIn("Plaintext links", first["gateway/routes.md"])

    def test_missing_metrics_rule_fails(self) -> None:
        rules = [rule for rule in self.rules if rule["destination"] != "gateway-metrics"]
        with self.assertRaisesRegex(ConfigurationError, "gateway-metrics"):
            traefik_static(self.platform(), rules)


if __name__ == "__main__":
    unittest.main()
