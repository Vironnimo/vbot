#!/usr/bin/env bash
# The complete backend suite on Linux for scripts/push.py, run inside WSL on the
# checked commit, which push.py has unpacked into "$1".
#
# As in CI: the packaged Python runtime and the complete shipped server dependency
# lock plus its fixed test-only overlay. Failed tests run once more for diagnosis;
# the original failure always keeps the run failed. The environment cache key
# includes both dependency locks and the base interpreter. push.py holds the cache
# lock throughout setup and execution.
set -u

checkout=$1
cache=$HOME/.cache/vbot-push
cd "$checkout" || exit 2
# WSL appends the Windows PATH; a Linux machine has no Windows shells to find.
PATH=$(printf %s "$PATH" | tr : '\n' | grep -v '^/mnt/' | paste -sd: -)

fail() {
    echo "linux tests: $*"
    exit 2
}

read -r runtime_url runtime_sha256 < <(python3 -c 'import json, platform
spec = json.load(open("scripts/linux/python.lock.json"))["runtimes"]["linux-" + platform.machine()]
print(spec["url"], spec["sha256"])')
[ -n "${runtime_sha256:-}" ] || fail "no locked Python runtime for $(uname -m)"

runtime=$cache/runtime-$runtime_sha256
if [ ! -x "$runtime/bin/python3" ]; then
    find "$cache" -maxdepth 1 -name 'runtime-*' -exec rm -rf {} +
    archive=$cache/runtime.tar.gz
    curl -fsSL --retry 3 "$runtime_url" -o "$archive" \
        && echo "$runtime_sha256  $archive" | sha256sum -c --quiet - \
        && mkdir "$runtime.partial" && tar -xzf "$archive" -C "$runtime.partial" \
        && mv "$runtime.partial/python" "$runtime" \
        || { rm -rf "$runtime" "$runtime.partial" "$archive"; fail "the locked Python runtime could not be installed"; }
    rm -rf "$runtime.partial" "$archive"
fi

key=$("$runtime/bin/python3" scripts/test_dependencies.py key) \
    || fail "dependency locks are stale or inconsistent"
venv=$cache/venv-$key
python=$venv/bin/python
if [ ! -x "$python" ]; then
    find "$cache" -maxdepth 1 -name 'venv-*' -exec rm -rf {} +
fi
"$runtime/bin/python3" scripts/test_dependencies.py install --environment "$venv" \
    || fail "the locked test environment could not be prepared"
export VIRTUAL_ENV="$venv"
export PATH="$venv/bin:$PATH"

native=resources/native
if [ -d "$cache/native" ]; then mkdir -p "$native" && cp -a "$cache/native/." "$native/"; fi
"$python" -m cli.search_runtime >/dev/null || fail "the search engine could not be provisioned"
rm -rf "$cache/native" && cp -a "$native" "$cache/native"
export TIKTOKEN_CACHE_DIR=$cache/tiktoken
"$python" -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')" \
    || fail "the token estimation encodings could not be provisioned"

last_failed() {
    "$python" -c 'import json, sys
try:
    print(*sorted(json.load(open(".pytest_cache/v/cache/lastfailed", encoding="utf-8"))), sep="\n")
except (OSError, ValueError):
    pass'
}

# Reuse CI's disjoint test shards, sequentially, so each process releases its
# retained state before the next group. Running the whole suite in one process
# can spend most of its time reclaiming memory under the WSL scope's budget.
workers=$("$python" -c 'from tests import cpu_pool; print(min(cpu_pool.LOCAL_AUTO_WORKERS, cpu_pool.pool_size()))') \
    || fail "the test core pool size could not be read"
run_shard() {
    local status=0 failed rerun=0 still
    echo "linux tests: shard $1/4"
    VBOT_TEST_SHARD="$1/4" "$python" -m pytest -n "$workers" --cache-clear -rfE -q --no-header || status=$?
    [ "$status" -eq 1 ] || return "$status"
    failed=$(last_failed)
    [ -n "$failed" ] || return 1
    echo "linux tests: running the $(echo "$failed" | wc -l) failed tests again, alone"
    "$python" -m pytest --last-failed --last-failed-no-failures none -n 0 -rfE -q --no-header || rerun=$?
    still=$(last_failed)
    while read -r test; do
        if [ "$rerun" -eq 0 ] || ! grep -qxF -- "$test" <<<"$still"; then echo "flaky: $test"; fi
    done <<<"$failed"
    return "$status"
}

status=0
for shard in 1 2 3 4; do
    run_shard "$shard" || status=$?
done
exit "$status"
