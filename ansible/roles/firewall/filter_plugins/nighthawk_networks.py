"""Address checks for the firewall role, on the standard library only."""

from __future__ import annotations

import ipaddress


def _network(value: object):
    return ipaddress.ip_network(str(value).strip(), strict=False)


def valid_network(value: object) -> bool:
    """Whether `value` is an address or a range in CIDR form."""
    try:
        _network(value)
    except ValueError:
        return False
    return True


def covers_everything(value: object) -> bool:
    """Whether a range admits every address of its family, such as 0.0.0.0/0."""
    return valid_network(value) and _network(value).prefixlen == 0


def in_any_network(address: object, networks: list) -> bool:
    """Whether `address` lies in at least one of `networks`."""
    try:
        candidate = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    return any(
        valid_network(network) and candidate.version == _network(network).version and candidate in _network(network)
        for network in networks
    )


def address_family(value: object) -> str:
    """`ipv4` or `ipv6`, as firewalld names them."""
    return f"ipv{_network(value).version}"


class FilterModule:
    def filters(self) -> dict:
        return {
            "nighthawk_valid_network": valid_network,
            "nighthawk_covers_everything": covers_everything,
            "nighthawk_in_any_network": in_any_network,
            "nighthawk_address_family": address_family,
        }
