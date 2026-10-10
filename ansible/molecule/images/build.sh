#!/bin/sh
# Build the test images the role scenarios run in, one per supported operating system.
# Usage: ansible/molecule/images/build.sh [container-command [family]]
#   container-command defaults to podman; family is debian or rhel, to build only those.
set -eu
runtime="${1:-podman}"
only="${2:-}"
here="$(cd "$(dirname "$0")" && pwd)"
build() {
    # name, family, base image
    if [ -n "$only" ] && [ "$only" != "$2" ]; then return 0; fi
    "$runtime" build --quiet --file "$here/Containerfile.$2" --build-arg "BASE=$3" --tag "localhost/nighthawk-test/$1" "$here"
}
build ubuntu:22.04 debian docker.io/library/ubuntu:22.04
build ubuntu:24.04 debian docker.io/library/ubuntu:24.04
build debian:12 debian docker.io/library/debian:12
build rocky:9 rhel docker.io/rockylinux/rockylinux:9
build almalinux:9 rhel docker.io/library/almalinux:9
