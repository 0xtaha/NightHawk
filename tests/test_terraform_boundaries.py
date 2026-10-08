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


def _mapped_network_rule_count(main_tf_text: str) -> int:
    """Count the rule objects inside environments/aws/main.tf's network_rules local."""
    match = re.search(r"network_rules\s*=\s*\[(.*?)\n  \]", main_tf_text, re.S)
    if match is None:
        raise AssertionError("could not find a network_rules literal in environments/aws/main.tf")
    return len(re.findall(r'\bid\s*=\s*"', match.group(1)))


class NetworkRuleParityTests(unittest.TestCase):
    """config/network.yaml's AWS-destined rules must stay mirrored in environments/aws/main.tf.

    A rule is AWS-destined when its `destination` is prefixed `aws-` (today,
    only `aws-s3`), matching the aws-vpc-network module's supported-
    destination allowlist. `scope` is not the right filter: `backend-object-
    storage` (scope `private`) and `remote-gateway` (scope `restricted-
    external`) are Docker/self-hosted traffic Terraform never provisions,
    despite not being `loopback` scope. See design.md's "Security groups are
    generated from a network_rules variable..." decision: the mapping is
    hand-maintained, so this test is the guardrail against a newly added
    AWS-destined rule silently going unmirrored.
    """

    def test_mapped_rule_count_matches_the_network_contract(self) -> None:
        rules = load_network(ROOT / "config" / "network.yaml")
        aws_scoped = [rule for rule in rules if rule["destination"].startswith("aws-")]
        mapped_count = _mapped_network_rule_count(AWS_ROOT_MAIN_TF.read_text(encoding="utf-8"))
        self.assertEqual(
            len(aws_scoped),
            mapped_count,
            "config/network.yaml declares a different number of AWS-scoped rules than "
            "terraform/environments/aws/main.tf's network_rules literal mirrors; update "
            "the mapping (see design.md) when a rule is added or removed.",
        )

    def test_mapped_rule_ids_match_the_network_contract(self) -> None:
        rules = load_network(ROOT / "config" / "network.yaml")
        aws_scoped_ids = {rule["id"] for rule in rules if rule["destination"].startswith("aws-")}
        main_tf_text = AWS_ROOT_MAIN_TF.read_text(encoding="utf-8")
        match = re.search(r"network_rules\s*=\s*\[(.*?)\n  \]", main_tf_text, re.S)
        assert match is not None
        mapped_ids = set(re.findall(r'id\s*=\s*"([a-z0-9-]+)"', match.group(1)))
        self.assertEqual(aws_scoped_ids, mapped_ids)


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
