#!/usr/bin/env bash
# Run in a clean checkout with Python 3.12+ and Node installed.
set -euo pipefail
cd "$(dirname "$0")/../.."
bash setup.sh
before=$(sha256sum .env)
bash setup.sh
[[ "$before" == "$(sha256sum .env)" ]]
bash start.sh --build-web
# Fail rather than accidentally testing a pre-existing service.
.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 8787)); s.close()'
# Isolate credentials and block the automatic scanner's outbound HTTP requests.
smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/funding-arb-smoke.XXXXXXXX")
server_pid=
cleanup() {
    if [[ -n "$server_pid" ]]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    rm -rf "$smoke_dir"
}
trap cleanup EXIT
env -i HOME="$smoke_dir" PATH="$PATH" FARB_DRY_RUN=1 \
HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 \
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
bash start.sh --api-only > "$smoke_dir/server.log" 2>&1 &
server_pid=$!
for _ in {1..30}; do
    if .venv/bin/python -c 'import json,urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8787/api/settings/strategy", timeout=1); assert r.status==200; assert isinstance(json.load(r),dict)' 2>/dev/null; then
        echo 'API smoke check passed'
        exit 0
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    sleep 1
done
cat "$smoke_dir/server.log" >&2
exit 1
