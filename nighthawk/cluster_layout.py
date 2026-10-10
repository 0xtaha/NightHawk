"""Whether a declared cluster layout can work, judged from plain data.

The `k3s_prerequisites` role gathers what each node reports and passes it here with what the
inventory declares. Every rule lives in this module so it can be tested without a host, and
every problem is returned, by node, rather than the first one.
"""

from __future__ import annotations

import ipaddress
import re

from nighthawk.config import ConfigurationError

SHAPES = ("development", "production")
# The cluster network's documented minimum.
MINIMUM_KERNEL = (5, 10)
# Loaded or loadable on every node: the container runtime's overlay filesystem and bridge
# filtering, and the tunnel the cluster network uses between nodes.
REQUIRED_MODULES = ("overlay", "br_netfilter", "vxlan")
# What the storage add-on installed in phase 6 needs on a node that offers storage.
STORAGE_MODULES = ("iscsi_tcp", "dm_crypt")
STORAGE_PACKAGES = ("open-iscsi", "nfs-common", "cryptsetup")
PRODUCTION_MINIMUM_SERVERS = 3
PRODUCTION_MINIMUM_STORAGE_NODES = 3


def _network(value: object, what: str, problems: list[str]) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(str(value), strict=True)
    except ValueError:
        problems.append(f"cluster: {what} {value!r} is not a network in address/prefix form")
        return None


def _pool_ranges(pool: object, problems: list[str]) -> list[tuple[ipaddress._BaseAddress, ipaddress._BaseAddress]]:
    """Each entry is a network or `first-last`; returns inclusive ranges."""
    ranges = []
    for entry in pool if isinstance(pool, list) else []:
        text = str(entry)
        try:
            if "-" in text:
                first, last = (ipaddress.ip_address(part.strip()) for part in text.split("-", 1))
                if first.version != last.version or last < first:
                    raise ValueError
            else:
                network = ipaddress.ip_network(text, strict=True)
                first, last = network[0], network[-1]
        except ValueError:
            problems.append(f"cluster: load-balancer pool entry {text!r} is neither a network nor a first-last range")
            continue
        ranges.append((first, last))
    return ranges


def _kernel(release: object) -> tuple[int, int] | None:
    match = re.match(r"(\d+)\.(\d+)", str(release))
    return (int(match.group(1)), int(match.group(2))) if match else None


def layout_problems(layout: dict) -> list[str]:
    """Every reason the layout cannot work, each starting with the node it concerns or `cluster`."""
    if not isinstance(layout, dict) or not isinstance(layout.get("nodes"), dict):
        raise ConfigurationError("the layout must be a mapping with a `nodes` mapping")
    problems: list[str] = []
    nodes: dict[str, dict] = layout["nodes"]
    shape = layout.get("shape")
    servers = sorted(name for name, node in nodes.items() if node.get("role") == "server")
    storage_nodes = sorted(name for name, node in nodes.items() if node.get("storage_disks"))
    for name, node in sorted(nodes.items()):
        if node.get("role") not in ("server", "agent"):
            problems.append(f"{name}: role {node.get('role')!r} is neither server nor agent")

    if shape not in SHAPES:
        problems.append(f"cluster: shape {shape!r} is not one of {', '.join(SHAPES)}")
    elif shape == "development":
        if len(nodes) != 1:
            problems.append(
                f"cluster: the development shape is exactly one node, but {len(nodes)} are declared; "
                "declare the production shape for several nodes"
            )
        elif not servers:
            problems.append("cluster: the single development node must be a server")
    else:
        if len(servers) < PRODUCTION_MINIMUM_SERVERS or len(servers) % 2 == 0:
            problems.append(
                f"cluster: the production shape needs an odd number of at least {PRODUCTION_MINIMUM_SERVERS} servers, "
                f"but {len(servers)} are declared ({', '.join(servers) or 'none'})"
            )
        if len(storage_nodes) < PRODUCTION_MINIMUM_STORAGE_NODES:
            problems.append(
                f"cluster: the production shape needs at least {PRODUCTION_MINIMUM_STORAGE_NODES} nodes that offer "
                f"storage, but {len(storage_nodes)} declare a storage disk ({', '.join(storage_nodes) or 'none'})"
            )

    node_network = _network(layout.get("node_network"), "the node network", problems)
    pod = _network(layout.get("pod_cidr"), "the pod range", problems)
    service = _network(layout.get("service_cidr"), "the service range", problems)
    named = (("the pod range", pod), ("the service range", service), ("the node network", node_network))
    for index, (first_name, first) in enumerate(named):
        for second_name, second in named[index + 1:]:
            if first is not None and second is not None and first.version == second.version and first.overlaps(second):
                problems.append(f"cluster: {first_name} {first} overlaps {second_name} {second}")

    addresses: dict[str, ipaddress._BaseAddress] = {}
    for name, node in sorted(nodes.items()):
        try:
            addresses[name] = ipaddress.ip_address(str(node.get("address")))
        except ValueError:
            problems.append(f"{name}: address {node.get('address')!r} is not an IP address")
            continue
        if node_network is not None and addresses[name] not in node_network:
            problems.append(f"{name}: address {addresses[name]} is outside the node network {node_network}")
    seen: dict[ipaddress._BaseAddress, str] = {}
    for name, address in addresses.items():
        if address in seen:
            problems.append(f"{name}: address {address} is also the address of {seen[address]}")
        seen.setdefault(address, name)

    ranges = _pool_ranges(layout.get("load_balancer_pool"), problems)
    if not ranges and not any("load-balancer pool entry" in problem for problem in problems):
        problems.append("cluster: the load-balancer address pool is empty")
    for first, last in ranges:
        label = str(first) if first == last else f"{first}-{last}"
        if node_network is not None and (
            first.version != node_network.version or first not in node_network or last not in node_network
        ):
            problems.append(f"cluster: load-balancer pool {label} is not inside the node network {node_network}")
        for name, address in addresses.items():
            if address.version == first.version and first <= address <= last:
                problems.append(f"{name}: address {address} is inside the load-balancer pool {label}")

    mtus = {name: node.get("mtu") for name, node in sorted(nodes.items())}
    declared_mtu = layout.get("mtu")
    if len(set(mtus.values())) > 1 or any(not isinstance(value, int) or isinstance(value, bool) for value in mtus.values()):
        listing = ", ".join(f"{name}={value}" for name, value in mtus.items())
        problems.append(f"cluster: the nodes' cluster-facing interfaces do not share one MTU: {listing}")
    elif declared_mtu is not None and mtus and set(mtus.values()) != {declared_mtu}:
        listing = ", ".join(f"{name}={value}" for name, value in mtus.items())
        problems.append(f"cluster: the inventory declares MTU {declared_mtu}, but the nodes report {listing}")

    for name, node in sorted(nodes.items()):
        kernel = _kernel(node.get("kernel"))
        if kernel is None:
            problems.append(f"{name}: kernel version {node.get('kernel')!r} cannot be read")
        elif kernel < MINIMUM_KERNEL:
            minimum = ".".join(map(str, MINIMUM_KERNEL))
            problems.append(f"{name}: kernel {node.get('kernel')} is older than {minimum}, which the cluster network needs")
        offers_storage = bool(node.get("storage_disks"))
        modules = set(node.get("modules") or [])
        wanted = REQUIRED_MODULES + (STORAGE_MODULES if offers_storage else ())
        missing = [module for module in wanted if module not in modules]
        if missing:
            problems.append(f"{name}: kernel module(s) not available: {', '.join(missing)}")
        if offers_storage:
            packages = set(node.get("packages") or [])
            absent = [package for package in STORAGE_PACKAGES if package not in packages]
            if absent:
                problems.append(f"{name}: package(s) the storage add-on needs are not installed: {', '.join(absent)}")
        devices = node.get("block_devices") or {}
        for disk in node.get("storage_disks") or []:
            device = devices.get(str(disk).removeprefix("/dev/"))
            if device is None:
                problems.append(f"{name}: storage disk {disk} does not exist")
            elif device.get("in_use"):
                problems.append(f"{name}: storage disk {disk} is in use ({device['in_use']})")
    return problems
