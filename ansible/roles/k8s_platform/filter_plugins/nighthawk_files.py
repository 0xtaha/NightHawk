"""Read a file on the control machine as a filter, so a list of paths can be mapped to their contents."""

from __future__ import annotations

from ansible.errors import AnsibleFilterError


def read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise AnsibleFilterError(f"{path}: cannot be read ({type(error).__name__})") from None


class FilterModule:
    def filters(self) -> dict:
        return {"nighthawk_read_file": read_file}
