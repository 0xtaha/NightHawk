from __future__ import annotations

import re
import unittest

from nighthawk.config import ROOT, load_network

AWS_ROOT_MAIN_TF = ROOT / "terraform" / "environments" / "aws" / "main.tf"

ZERO_RESOURCE_ROOTS = (
    ROOT / "terraform" / "environments" / "self-hosted-k8s",
    ROOT / "terraform" / "environments" / "docker",
)

RESOURCE_BLOCK_RE = re.compile(r'\bresource\s+"[^"]+"\s+"[^"]+"\s*\{')


RULE_FIELDS = ("source", "destination", "protocol", "port", "scope")


def mirrored_rules(main_tf_text: str) -> dict[str, dict]:
    """The rule objects inside environments/aws/main.tf's network_rules local, by ID."""
    match = re.search(r"network_rules\s*=\s*\[(.*?)\n  \]", main_tf_text, re.S)
    if match is None:
        raise AssertionError("could not find a network_rules literal in environments/aws/main.tf")
    rules: dict[str, dict] = {}
    for body in re.findall(r"\{(.*?)\}", match.group(1), re.S):
        fields = {
            name: int(number) if number else text
            for name, text, number in re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([0-9]+))', body)
        }
        rules[fields.pop("id")] = fields
    return rules


def parity_problems(contract_rules: list[dict], mirrored: dict[str, dict]) -> list[str]:
    """Every difference between the contract's AWS-destined rules and their Terraform mirror."""
    expected = {
        rule["id"]: {field: rule[field] for field in RULE_FIELDS}
        for rule in contract_rules if rule["destination"].startswith("aws-")
    }
    problems = [f"{rule_id}: declared in config/network.yaml but not mirrored" for rule_id in sorted(expected.keys() - mirrored.keys())]
    problems += [f"{rule_id}: mirrored but not an AWS-destined rule of config/network.yaml" for rule_id in sorted(mirrored.keys() - expected.keys())]
    for rule_id in sorted(expected.keys() & mirrored.keys()):
        for field in sorted(set(RULE_FIELDS) | mirrored[rule_id].keys()):
            wanted, found = expected[rule_id].get(field), mirrored[rule_id].get(field)
            if wanted != found:
                problems.append(f"{rule_id}: {field} is {found!r} in the mirror but {wanted!r} in the contract")
    return problems


class NetworkRuleParityTests(unittest.TestCase):
    """config/network.yaml's AWS-destined rules must stay mirrored in environments/aws/main.tf, field by field.

    A rule is AWS-destined when its `destination` is prefixed `aws-` (today,
    only `aws-s3`), matching the aws-vpc-network module's supported-
    destination allowlist. `scope` is not the right filter: `backend-object-
    storage` (scope `private`) and `remote-gateway` (scope `restricted-
    external`) are Docker/self-hosted traffic Terraform never provisions,
    despite not being `loopback` scope. The mapping is hand-maintained, so
    these tests are the guardrail against a rule that is added, removed, or
    changed in one place only.
    """

    def setUp(self) -> None:
        self.contract = load_network(ROOT / "config" / "network.yaml")
        self.text = AWS_ROOT_MAIN_TF.read_text(encoding="utf-8")

    def test_every_field_of_every_mirrored_rule_matches_the_contract(self) -> None:
        self.assertEqual(parity_problems(self.contract, mirrored_rules(self.text)), [])

    def test_the_mirror_is_not_empty(self) -> None:
        self.assertEqual(sorted(mirrored_rules(self.text)), ["aws-object-storage"])

    def test_a_drift_in_any_single_field_names_the_rule_and_the_field(self) -> None:
        changes = {
            "port": ("port        = 443", "port        = 8443"),
            "protocol": ('protocol    = "tcp"', 'protocol    = "udp"'),
            "source": ('source      = "signal-backend"', 'source      = "gateway"'),
            "destination": ('destination = "aws-s3"', 'destination = "aws-other"'),
            "scope": ('scope       = "private"', 'scope       = "restricted-external"'),
        }
        for field, (old, new) in changes.items():
            with self.subTest(field=field):
                self.assertEqual(self.text.count(old), 1, f"{field}: the literal changed shape; update this test")
                (problem,) = parity_problems(self.contract, mirrored_rules(self.text.replace(old, new)))
                self.assertIn("aws-object-storage", problem)
                self.assertIn(f": {field} is ", problem)

    def test_added_removed_and_extra_fields_are_reported(self) -> None:
        mirrored = mirrored_rules(self.text)
        self.assertEqual(parity_problems(self.contract, {}), ["aws-object-storage: declared in config/network.yaml but not mirrored"])
        extra = {**mirrored, "invented": dict(mirrored["aws-object-storage"])}
        self.assertEqual(parity_problems(self.contract, extra), ["invented: mirrored but not an AWS-destined rule of config/network.yaml"])
        with_field = {"aws-object-storage": {**mirrored["aws-object-storage"], "cidr": "0.0.0.0/0"}}
        self.assertEqual(
            parity_problems(self.contract, with_field),
            ["aws-object-storage: cidr is '0.0.0.0/0' in the mirror but None in the contract"],
        )


class ZeroResourceRootTests(unittest.TestCase):
    """The self-hosted-k8s and docker environment roots must declare no resources.

    Per design.md, these profiles' local infrastructure (k3s/Cilium/MetalLB/
    Longhorn/Traefik/cert-manager, and Compose/SeaweedFS respectively) is
    entirely owned by Ansible/Helm, not Terraform. These roots exist only to
    carry a pinned terraform/aws version pair for the compatibility matrix.
    This test fails the build if a `resource "..." "..." { ... }` block is
    ever added to either root, which would be a design change requiring an
    OpenSpec proposal, not a silent edit.
    """

    def test_no_resource_blocks_declared(self) -> None:
        for root_dir in ZERO_RESOURCE_ROOTS:
            for tf_file in sorted(root_dir.glob("*.tf")):
                text = tf_file.read_text(encoding="utf-8")
                matches = RESOURCE_BLOCK_RE.findall(text)
                self.assertEqual(
                    matches,
                    [],
                    f"{tf_file.relative_to(ROOT)} declares a resource block "
                    f"({matches!r}), but this root must stay zero-resource; "
                    "see its README.md and design.md.",
                )


if __name__ == "__main__":
    unittest.main()
