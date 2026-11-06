#!/bin/bash
# bundle httpfs so cold starts don't download it

PACKAGE_DIR=$1

if [[ "${TARGET_ARCHITECTURE}" == "arm64" ]]; then
  PLATFORM="linux_arm64"
else
  PLATFORM="linux_amd64"
fi

VERSION="v$(sed -n 's/^duckdb==//p' requirements.txt)"
EXTENSION_DIR="${PACKAGE_DIR}/duckdb_extensions/${VERSION}/${PLATFORM}"
EXTENSION_URL="http://extensions.duckdb.org/${VERSION}/${PLATFORM}/httpfs.duckdb_extension.gz"

mkdir -p "${EXTENSION_DIR}"
python3 - "${EXTENSION_URL}" "${EXTENSION_DIR}/httpfs.duckdb_extension" <<'EOF'
import gzip
import shutil
import sys
import urllib.request

# the default urllib user agent gets a 403
request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "SeBS-benchmark-build"})
with urllib.request.urlopen(request) as response, open(sys.argv[2], "wb") as output:
    with gzip.GzipFile(fileobj=response) as extension:
        shutil.copyfileobj(extension, output)
EOF
