"""Static checks of the Ansible tree with the pinned tools, when they are installed."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from nighthawk.config import ROOT

ANSIBLE = ROOT / "ansible"
TOOLS = ANSIBLE / ".venv" / "bin"
REASON = (
    "the Ansible development tools are absent: create ansible/.venv from ansible/requirements-dev.txt "
    "and install ansible/requirements.yml into ansible/.collections"
)


def tool(name: str) -> str | None:
    local = TOOLS / name
    return str(local) if local.exists() else shutil.which(name)


def run(command: list[str]) -> subprocess.CompletedProcess:
    environment = {**os.environ, "PATH": f"{TOOLS}{os.pathsep}{os.environ.get('PATH', '')}", "ANSIBLE_FORCE_COLOR": "0",
                   "NO_COLOR": "1", "ANSIBLE_NOCOWS": "1"}
    # Ansible refuses a non-blocking stderr, which some runners hand to child processes.
    return subprocess.run(
        command, cwd=ANSIBLE, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
    )


@unittest.skipUnless(tool("ansible-lint") and tool("ansible-playbook") and (ANSIBLE / ".collections").exists(), REASON)
class AnsibleStaticChecks(unittest.TestCase):
    def test_ansible_lint_is_clean(self) -> None:
        result = run([tool("ansible-lint"), "--nocolor", "--offline", "."])
        self.assertEqual(result.returncode, 0, result.stdout[-6000:])

    def test_every_playbook_passes_the_syntax_check(self) -> None:
        playbooks = sorted((ANSIBLE / "playbooks").glob("*.yml"))
        self.assertTrue(playbooks)
        inventory = ANSIBLE / "inventories" / "docker-host.example"
        for playbook in playbooks:
            with self.subTest(playbook=playbook.name):
                result = run([
                    tool("ansible-playbook"), "--syntax-check", "--inventory", str(inventory),
                    "--extra-vars", "nighthawk_inputs_file=/dev/null", str(playbook),
                ])
                self.assertEqual(result.returncode, 0, result.stdout[-4000:])

    def test_every_example_inventory_parses(self) -> None:
        inventories = sorted(path for path in (ANSIBLE / "inventories").iterdir() if path.is_dir())
        self.assertEqual([path.name for path in inventories], [
            "docker-host.example", "external-collector.example", "k3s-development.example", "k3s-production.example",
        ])
        for inventory in inventories:
            with self.subTest(inventory=inventory.name):
                result = run([tool("ansible-inventory"), "--inventory", str(inventory), "--list"])
                self.assertEqual(result.returncode, 0, result.stdout[-4000:])
                self.assertIn("nighthawk_host_role", result.stdout)


if __name__ == "__main__":
    unittest.main()
