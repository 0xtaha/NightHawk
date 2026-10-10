#!/bin/sh
# Run role scenarios in containers of each supported operating system.
# Usage: ansible/molecule/run.sh [role ...]      (default: every role that has a scenario)
# Needs the development tools (ansible/requirements-dev.txt), the collections
# (ansible/requirements.yml), and the test images (ansible/molecule/images/build.sh).
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
ansible_dir="$(dirname "$here")"
repository="$(dirname "$ansible_dir")"
python="${NIGHTHAWK_PYTHON:-$repository/.venv/bin/python}"
if [ -d "$ansible_dir/.venv/bin" ]; then
    PATH="$ansible_dir/.venv/bin:$PATH"
fi
export PATH NIGHTHAWK_ANSIBLE_DIR="$ansible_dir" NIGHTHAWK_REPOSITORY_DIR="$repository" NIGHTHAWK_PYTHON="$python"

# The same rendered inputs an operator would use, from the example platform document.
rendered="$here/.rendered"
rm -rf "$rendered"
(cd "$repository" && "$python" -m nighthawk render-contracts \
    --config "${NIGHTHAWK_TEST_CONFIG:-config/tenants.example.yaml}" --output "$rendered" >/dev/null)
export NIGHTHAWK_INPUTS_FILE="$rendered/ansible/nighthawk.yml"
# A collector for a machine outside the platform, as the alloy_collector scenario installs it.
sed 's/entry_points: \[local-gateway, grafana-gateway\]/entry_points: [local-gateway, grafana-gateway, remote-gateway]/' \
    "$repository/config/tenants.example.yaml" > "$rendered/platform-remote.yaml"
(cd "$repository" && "$python" -m nighthawk render-collector --config "$rendered/platform-remote.yaml" \
    --tenant example --datastream application --profile vm --entry-point remote-gateway \
    --output "$rendered/collector-vm" >/dev/null)
export NIGHTHAWK_COLLECTOR_DIR="$rendered/collector-vm"

if [ "$#" -eq 0 ]; then
    set -- $(cd "$ansible_dir/roles" && for role in *; do [ -d "$role/molecule" ] && echo "$role"; done)
fi
status=0
for role in "$@"; do
    for scenario in "$ansible_dir/roles/$role/molecule"/*/; do
        name="$(basename "$scenario")"
        echo "=== $role / $name"
        (cd "$ansible_dir/roles/$role" && molecule --base-config "$here/base.yml" test --scenario-name "$name") || status=1
    done
done
exit "$status"
