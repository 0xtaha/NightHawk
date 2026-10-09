#!/bin/sh
# Create every bucket named in /etc/nighthawk/storage/buckets.txt, then confirm each exists.
# Creating an existing bucket is a no-op, so this is safe to repeat.
set -eu

master="${NIGHTHAWK_STORAGE_MASTER:?}"
buckets_file=/etc/nighthawk/storage/buckets.txt

while IFS= read -r bucket; do
  [ -n "$bucket" ] || continue
  echo "s3.bucket.create -name $bucket" | weed shell -master="$master" >/dev/null
done < "$buckets_file"

listing=$(echo "s3.bucket.list" | weed shell -master="$master")
missing=0
while IFS= read -r bucket; do
  [ -n "$bucket" ] || continue
  if ! printf '%s\n' "$listing" | grep -q "^  $bucket[[:space:]]"; then
    echo "storage-init: bucket $bucket was not created" >&2
    missing=1
  fi
done < "$buckets_file"
[ "$missing" -eq 0 ] || exit 1
echo "storage-init: all buckets present"
