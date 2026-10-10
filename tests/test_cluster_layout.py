from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from nighthawk.__main__ import main
from nighthawk.cluster_layout import (
    MINIMUM_KERNEL, REQUIRED_MODULES, STORAGE_MODULES, STORAGE_PACKAGES, layout_problems,
)
from nighthawk.config import ConfigurationError

MODULES = [*REQUIRED_MODULES, *STORAGE_MODULES]


def node(role: str, address: str, storage: bool = True) -> dict:
    return {
        "role": role, "address": address, "kernel": "6.8.0-45-generic", "mtu": 1500,
        "modules": list(MODULES), "packages": list(STORAGE_PACKAGES),
        "storage_disks": ["/dev/sdb"] if storage else [],
        "block_devices": {"sda": {"in_use": "mounted at /"}, "sdb": {"in_use": ""}},
    }


def production() -> dict:
    return {
        "shape": "production", "node_network": "192.0.2.0/24", "pod_cidr": "10.42.0.0/16",
        "service_cidr": "10.43.0.0/16", "mtu": 1500, "load_balancer_pool": ["192.0.2.200-192.0.2.220"],
        "nodes": {
            **{f"server-{index}": node("server", f"192.0.2.3{index}", storage=False) for index in (1, 2, 3)},
            **{f"agent-{index}": node("agent", f"192.0.2.4{index}") for index in (1, 2, 3)},
        },
    }


def development() -> dict:
    layout = production()
    layout["shape"] = "development"
    layout["nodes"] = {"dev-1": node("server", "192.0.2.20")}
    return layout


class LayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layout = production()

    def problems(self) -> list[str]:
        return layout_problems(copy.deepcopy(self.layout))

    def assert_one(self, expected: str) -> None:
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(expected, problems[0])

    def test_layouts_that_can_work_have_no_problem(self) -> None:
        self.assertEqual(self.problems(), [])
        self.assertEqual(layout_problems(development()), [])

    def test_production_needs_an_odd_number_of_at_least_three_servers(self) -> None:
        del self.layout["nodes"]["server-3"]
        self.assert_one("needs an odd number of at least 3 servers, but 2 are declared (server-1, server-2)")
        self.layout["nodes"]["server-3"] = node("server", "192.0.2.33", storage=False)
        self.layout["nodes"]["server-4"] = node("server", "192.0.2.34", storage=False)
        self.assert_one("but 4 are declared")

    def test_production_needs_three_storage_nodes(self) -> None:
        self.layout["nodes"]["agent-3"]["storage_disks"] = []
        self.assert_one("at least 3 nodes that offer storage, but 2 declare a storage disk (agent-1, agent-2)")

    def test_development_is_exactly_one_server(self) -> None:
        self.layout["shape"] = "development"
        self.assert_one("the development shape is exactly one node, but 6 are declared; declare the production shape")
        self.layout = development()
        self.layout["nodes"]["dev-1"]["role"] = "agent"
        self.assert_one("the single development node must be a server")

    def test_unknown_shape_and_role_are_named(self) -> None:
        self.layout["shape"] = "large"
        self.layout["nodes"]["agent-1"]["role"] = "worker"
        problems = self.problems()
        self.assertTrue(any("shape 'large' is not one of development, production" in problem for problem in problems))
        self.assertTrue(any(problem.startswith("agent-1: role 'worker'") for problem in problems))

    def test_old_or_unreadable_kernel_is_named_by_node(self) -> None:
        self.layout["nodes"]["agent-2"]["kernel"] = "5.4.0-200-generic"
        minimum = ".".join(map(str, MINIMUM_KERNEL))
        self.assert_one(f"agent-2: kernel 5.4.0-200-generic is older than {minimum}")
        self.layout["nodes"]["agent-2"]["kernel"] = "unknown"
        self.assert_one("agent-2: kernel version 'unknown' cannot be read")

    def test_missing_modules_are_listed_and_storage_modules_only_matter_on_storage_nodes(self) -> None:
        self.layout["nodes"]["server-1"]["modules"] = ["overlay"]
        self.assert_one("server-1: kernel module(s) not available: br_netfilter, vxlan")
        self.layout["nodes"]["server-1"]["modules"] = list(REQUIRED_MODULES)
        self.assertEqual(self.problems(), [])
        self.layout["nodes"]["agent-1"]["modules"] = list(REQUIRED_MODULES)
        self.assert_one("agent-1: kernel module(s) not available: iscsi_tcp, dm_crypt")

    def test_storage_packages_are_required_on_storage_nodes_only(self) -> None:
        self.layout["nodes"]["server-1"]["packages"] = []
        self.assertEqual(self.problems(), [])
        self.layout["nodes"]["agent-3"]["packages"] = ["nfs-common", "cryptsetup"]
        self.assert_one("agent-3: package(s) the storage add-on needs are not installed: open-iscsi")

    def test_storage_disk_missing_or_in_use_names_node_and_disk(self) -> None:
        self.layout["nodes"]["agent-1"]["storage_disks"] = ["/dev/sdc"]
        self.assert_one("agent-1: storage disk /dev/sdc does not exist")
        self.layout["nodes"]["agent-1"]["storage_disks"] = ["/dev/sda"]
        self.assert_one("agent-1: storage disk /dev/sda is in use (mounted at /)")

    def test_mismatched_mtu_lists_each_node(self) -> None:
        self.layout["nodes"]["agent-2"]["mtu"] = 9000
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        for name, value in (("agent-1", 1500), ("agent-2", 9000), ("server-3", 1500)):
            self.assertIn(f"{name}={value}", problems[0])
        self.layout["nodes"]["agent-2"]["mtu"] = 1500
        self.layout["mtu"] = 9000
        self.assert_one("the inventory declares MTU 9000, but the nodes report")

    def test_overlapping_ranges_are_rejected(self) -> None:
        self.layout["pod_cidr"] = "192.0.2.0/25"
        self.assert_one("the pod range 192.0.2.0/25 overlaps the node network 192.0.2.0/24")
        self.layout["pod_cidr"] = "10.43.0.0/17"
        self.assert_one("the pod range 10.43.0.0/17 overlaps the service range 10.43.0.0/16")
        self.layout["pod_cidr"] = "10.42.0.1/16"
        self.assert_one("the pod range '10.42.0.1/16' is not a network in address/prefix form")

    def test_address_pool_must_be_non_empty_inside_the_node_network_and_free_of_nodes(self) -> None:
        self.layout["load_balancer_pool"] = []
        self.assert_one("the load-balancer address pool is empty")
        self.layout["load_balancer_pool"] = ["198.51.100.10-198.51.100.20"]
        self.assert_one("load-balancer pool 198.51.100.10-198.51.100.20 is not inside the node network 192.0.2.0/24")
        self.layout["load_balancer_pool"] = ["192.0.2.40-192.0.2.42"]
        problems = self.problems()
        self.assertEqual(
            problems,
            [f"agent-{index}: address 192.0.2.4{index} is inside the load-balancer pool 192.0.2.40-192.0.2.42" for index in (1, 2)],
        )
        self.layout["load_balancer_pool"] = ["192.0.2.32/31"]
        self.assertEqual(len(self.problems()), 2)
        self.layout["load_balancer_pool"] = ["192.0.2.220-192.0.2.200"]
        self.assert_one("pool entry '192.0.2.220-192.0.2.200' is neither a network nor a first-last range")

    def test_node_addresses_must_be_distinct_and_in_the_node_network(self) -> None:
        self.layout["nodes"]["agent-1"]["address"] = "198.51.100.5"
        self.assert_one("agent-1: address 198.51.100.5 is outside the node network 192.0.2.0/24")
        self.layout["nodes"]["agent-1"]["address"] = "192.0.2.42"
        self.assert_one("agent-2: address 192.0.2.42 is also the address of agent-1")

    def test_several_problems_across_nodes_are_all_reported(self) -> None:
        del self.layout["nodes"]["server-3"]
        self.layout["nodes"]["agent-1"]["storage_disks"] = ["/dev/sdc"]
        self.layout["nodes"]["agent-2"]["mtu"] = 9000
        self.layout["nodes"]["server-1"]["kernel"] = "4.19.0"
        self.layout["load_balancer_pool"] = ["192.0.2.41"]
        problems = self.problems()
        self.assertEqual(len(problems), 5, problems)
        for start in ("cluster: the production shape needs an odd number", "agent-1: storage disk", "cluster: the nodes'",
                      "server-1: kernel", "agent-1: address 192.0.2.41 is inside"):
            self.assertTrue(any(problem.startswith(start) for problem in problems), start)

    def test_malformed_input_is_an_error_not_a_pass(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must be a mapping with a `nodes` mapping"):
            layout_problems({"shape": "production"})


class CommandLineTests(unittest.TestCase):
    def run_cli(self, layout: object) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            path.write_text(layout if isinstance(layout, str) else json.dumps(layout), encoding="utf-8")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                status = main(["check-cluster-layout", "--layout", str(path)])
        return status, output.getvalue(), errors.getvalue()

    def test_working_layout_passes(self) -> None:
        status, output, errors = self.run_cli(production())
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("6 node(s)", output)

    def test_problems_are_printed_one_per_line_and_nothing_else_happens(self) -> None:
        layout = production()
        layout["nodes"]["agent-1"]["mtu"] = 1400
        layout["load_balancer_pool"] = []
        status, output, errors = self.run_cli(layout)
        self.assertEqual((status, output), (1, ""))
        lines = errors.strip().splitlines()
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("2 problem(s)", lines[0])
        self.assertIn("No host was changed", lines[0])

    def test_layout_is_read_from_standard_input(self) -> None:
        from unittest import mock
        output = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(development()))), redirect_stdout(output):
            status = main(["check-cluster-layout", "--layout", "-"])
        self.assertEqual(status, 0)
        self.assertIn("1 node(s), shape development", output.getvalue())

    def test_unreadable_layout_fails(self) -> None:
        status, _, errors = self.run_cli("{not json")
        self.assertEqual(status, 1)
        self.assertIn("error:", errors)
