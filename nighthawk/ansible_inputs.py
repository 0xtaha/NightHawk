"""Non-secret inputs for the Ansible automation, rendered from the contracts and the matrix.

Roles and playbooks take every port, version, checksum, and supported system from this
output. They never read config/*.yaml themselves, and nothing here contacts Vault.
"""

from __future__ import annotations

import yaml

from nighthawk import cluster_layout
from nighthawk.config import ConfigurationError, Platform, mapping, sequence, string

# Who may connect, as a role resolves it from its inventory: an allowlist the operator
# supplies, or the addresses of the cluster's own nodes.
SOURCE_GROUPS = {
    "administrator": "admin_allowlist",
    "authorized-collector": "collector_allowlist",
    "cluster-node": "cluster_nodes",
    "cluster-server": "cluster_servers",
}
# Sources that are pods. Their connections to a node's own ports come through the cluster
# network on that node, which the host firewall's inbound allowlists do not govern; the
# NetworkPolicies generated from the same rules do.
POD_SOURCES = ("collector", "cluster-operator", "gateway", "pyroscope")
# Which inbound destinations exist on a host of each role.
ROLE_DESTINATIONS = {
    "docker_host": ("host-ssh", "gateway"),
    "cluster_server": ("host-ssh", "cluster-server", "cluster-node"),
    "cluster_agent": ("host-ssh", "cluster-node"),
    "collector_host": ("host-ssh",),
}
# Which operating system set of the matrix each role is checked against.
ROLE_OS_SUPPORT = {
    "docker_host": "docker_hosts", "collector_host": "docker_hosts",
    "cluster_server": "k3s_nodes", "cluster_agent": "k3s_nodes",
}
# Kernel network settings the hardening role applies everywhere.
HARDENING_SYSCTL = {
    "net.ipv4.conf.all.accept_redirects": 0,
    "net.ipv4.conf.all.accept_source_route": 0,
    "net.ipv4.conf.all.send_redirects": 0,
    "net.ipv4.conf.default.accept_redirects": 0,
    "net.ipv4.conf.default.accept_source_route": 0,
    "net.ipv4.icmp_echo_ignore_broadcasts": 1,
    "net.ipv4.tcp_syncookies": 1,
    "net.ipv6.conf.all.accept_redirects": 0,
    "net.ipv6.conf.default.accept_redirects": 0,
}
# Settings that depend on what runs on the host. A container runtime and a cluster network
# both forward packets; the cluster network also needs reverse-path filtering off, because it
# routes pod traffic asymmetrically across its own interfaces.
ROLE_SYSCTL = {
    "docker_host": {"net.ipv4.ip_forward": 1, "net.ipv4.conf.all.rp_filter": 2},
    "cluster_server": {"net.ipv4.ip_forward": 1, "net.ipv4.conf.all.rp_filter": 0},
    "cluster_agent": {"net.ipv4.ip_forward": 1, "net.ipv4.conf.all.rp_filter": 0},
    "collector_host": {"net.ipv4.ip_forward": 0, "net.ipv4.conf.all.rp_filter": 1},
}


def firewall_rules(rules: list[dict]) -> dict[str, list[dict]]:
    """Inbound rules per host role. A loopback rule is never opened, and on a Docker host
    only the gateway's externally scoped entry point is: its private flows stay between containers."""
    by_role: dict[str, list[dict]] = {}
    for role, destinations in ROLE_DESTINATIONS.items():
        selected = []
        for rule in rules:
            if rule["destination"] not in destinations or rule["scope"] == "loopback":
                continue
            if rule["destination"] == "gateway" and rule["scope"] != "restricted-external":
                continue
            if rule["source"] in POD_SOURCES:
                continue
            if rule["source"] not in SOURCE_GROUPS:
                raise ConfigurationError(
                    f"network rule {rule['id']}: no inventory source is defined for {rule['source']!r} on a {role}"
                )
            selected.append({
                "id": rule["id"], "protocol": rule["protocol"], "port": rule["port"],
                "source": SOURCE_GROUPS[rule["source"]], "scope": rule["scope"], "purpose": rule["purpose"],
            })
        by_role[role] = sorted(selected, key=lambda item: item["id"])
    return by_role


def ansible_inputs(platform: Platform, rules: list[dict], matrix: dict) -> dict:
    artifacts = mapping(matrix["host_artifacts"])
    docker = mapping(artifacts["docker_engine"])
    cluster = mapping(matrix["kubernetes_platform"])
    entry_points = {entry.scope: entry.port for entry in platform.gateway.entry_points}
    os_support = mapping(matrix["os_support"])
    ports = {rule["id"]: rule["port"] for rule in rules}
    return {"nighthawk": {
        "deployment": platform.deployment,
        "profile": platform.profile,
        "gateway": {
            "hostname": platform.gateway.hostname,
            "grafana_hostname": platform.grafana.hostname,
            "loopback_port": entry_points.get("loopback"),
            "external_port": entry_points.get("restricted-external"),
        },
        "firewall": firewall_rules(rules),
        # What the cluster roles need beyond firewall rules: the ports they configure, and what
        # the layout check asks every node about.
        "cluster": {
            "api_port": ports.get("cluster-api-server"),
            "overlay_port": ports.get("cluster-network-overlay"),
            "checked_modules": [*cluster_layout.REQUIRED_MODULES, *cluster_layout.STORAGE_MODULES],
            "checked_packages": list(cluster_layout.STORAGE_PACKAGES),
        },
        "sysctl": {role: {**HARDENING_SYSCTL, **settings} for role, settings in sorted(ROLE_SYSCTL.items())},
        "os_support": {
            role: [
                {"distribution": string(mapping(entry)["distribution"]),
                 "versions": [string(item) for item in sequence(mapping(entry)["versions"])],
                 "architectures": [string(item) for item in sequence(mapping(entry)["architectures"])]}
                for entry in sequence(os_support[target])
            ]
            for role, target in sorted(ROLE_OS_SUPPORT.items())
        },
        "pins": {
            "docker_engine": {
                "version": string(docker["version"]),
                "compose_plugin_version": string(docker["compose_plugin_version"]),
                "signing_key_fingerprints": dict(mapping(docker["signing_key_fingerprints"])),
            },
            "k3s": {
                "version": string(mapping(cluster["k3s"])["version"]),
                "sha256": dict(mapping(mapping(artifacts["k3s"])["sha256"])),
            },
            "cilium": {"version": string(mapping(cluster["cilium"])["version"])},
            "alloy": {
                "version": string(mapping(mapping(matrix["collectors"])["alloy"])["version"]),
                "sha256": dict(mapping(mapping(artifacts["alloy"])["sha256"])),
            },
        },
    }}


def render_ansible_inputs(platform: Platform, rules: list[dict], matrix: dict) -> dict[str, str]:
    """The one variables file every playbook loads."""
    header = (
        "# Generated by `nighthawk render-contracts`; do not edit. Re-render after a change to the\n"
        "# platform document, config/network.yaml, or config/versions.yaml.\n"
    )
    body = yaml.safe_dump(ansible_inputs(platform, rules, matrix), sort_keys=True, default_flow_style=False)
    return {"ansible/nighthawk.yml": header + body}
