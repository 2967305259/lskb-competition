#!/bin/bash
# Run a new disposable MySQL container and use only its generated test databases.
set -euo pipefail
cd "$(dirname "$0")/.."
name_args=()
if [ -n "${P0_MYSQL_CONTAINER:-}" ]; then
    name_args=(--name "$P0_MYSQL_CONTAINER")
fi
container=$(docker run -d "${name_args[@]}" --tmpfs /var/lib/mysql \
    -e MYSQL_ROOT_PASSWORD=p0-test-only -p "127.0.0.1:${P0_MYSQL_PORT:-}:3306" mysql:8.0)
trap 'docker rm -f "$container" >/dev/null' EXIT
export P0_MYSQL_CONTAINER="$container"
published_address=$(docker port "$container" 3306/tcp)
P0_TEST_PORT=${published_address##*:}
ready=0
for attempt in $(seq 1 60); do
    if docker exec -e MYSQL_PWD=p0-test-only "$container" mysql -h127.0.0.1 -uroot -e 'SELECT 1' >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" = 1 ] || { echo 'MySQL startup failed'; exit 1; }
export PATH="$PWD/tests/mysql_tools:$PATH"
export P0_MYSQL_URL="mysql+pymysql://root:p0-test-only@127.0.0.1:$P0_TEST_PORT"
export P0_MYSQLDUMP="$PWD/tests/mysql_tools/mysqldump"
"${PYTHON:-python}" -m pytest -q --disable-warnings "$@"
