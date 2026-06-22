#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "error: drop_caches.sh must be run as root" >&2
    exit 1
fi

sync
printf '3\n' > /proc/sys/vm/drop_caches
