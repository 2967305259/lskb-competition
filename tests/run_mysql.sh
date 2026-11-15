#!/bin/bash
# Run only against a newly created disposable MySQL container; never use a business database.
set -euo pipefail
cd "$(dirname "$0")/.."
export P0_MYSQL_CONTAINER=${P0_MYSQL_CONTAINER:-lskb-p0-mysql-test}
P0_TEST_PORT=${P0_MYSQL_PORT:-13361}
docker run -d --name "$P0_MYSQL_CONTAINER" --tmpfs /var/lib/mysql \
    -e MYSQL_ROOT_PASSWORD=p0-test-only -p "127.0.0.1:$P0_TEST_PORT:3306" mysql:8.0 >/dev/null
trap 'docker rm -f "$P0_MYSQL_CONTAINER" >/dev/null' EXIT
ready=0
for attempt in $(seq 1 60); do
    if docker exec -e MYSQL_PWD=p0-test-only "$P0_MYSQL_CONTAINER" mysqladmin ping -h127.0.0.1 -uroot --silent >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
done
[ "$ready" = 1 ] || { echo 'MySQL startup failed'; exit 1; }
export PATH="$PWD/tests/mysql_tools:$PATH"
export P0_MYSQL_URL="mysql+pymysql://root:p0-test-only@127.0.0.1:$P0_TEST_PORT"
export P0_MYSQLDUMP="$PWD/tests/mysql_tools/mysqldump"
"${PYTHON:-python}" -m pytest -q --disable-warnings "$@"
