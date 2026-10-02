#!/usr/bin/env bash
# The complete backend suite on Linux for scripts/push.py, run inside WSL on the
# checked commit, which push.py has unpacked into "$1".
#
# As in CI: the Python runtime the Linux packages bundle
# (scripts/linux/python.lock.json) with the dev extra installed editable, the
# pinned search engine and the token estimation encodings provisioned first,
# and failed tests run once more alone; those that pass then are printed as
# "flaky: <node id>" and do not fail the run. Everything lasting lives in
# ~/.cache/vbot-push: uv (the version pyproject.toml pins), the runtime, one
# environment per pyproject.toml and runtime, the search engine and the
# encodings. push.py holds that directory's lock for the whole run.
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

uv_version=$(grep -o '"uv==[0-9.]*"' pyproject.toml | head -1 | tr -d '"' | cut -d= -f3)
[ -n "$uv_version" ] || fail "the uv version pin was not found"
read -r runtime_url runtime_sha256 < <(python3 -c 'import json, platform
spec = json.load(open("scripts/linux/python.lock.json"))["runtimes"]["linux-" + platform.machine()]
print(spec["url"], spec["sha256"])')
[ -n "${runtime_sha256:-}" ] || fail "no locked Python runtime for $(uname -m)"

uv=$cache/uv-$uv_version/bin/uv
if [ ! -x "$uv" ]; then
    rm -rf "$cache/uv-$uv_version"
    python3 -m venv "$cache/uv-$uv_version" \
        && "$cache/uv-$uv_version/bin/pip" install -q "uv==$uv_version" \
        || fail "uv $uv_version could not be installed (needs python3 with venv)"
fi

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

key=$( (cat pyproject.toml; echo "$runtime_sha256") | sha256sum | cut -c1-16)
venv=$cache/venv-$key
python=$venv/bin/python
if [ ! -x "$python" ]; then
    find "$cache" -maxdepth 1 -name 'venv-*' -exec rm -rf {} +
    "$uv" venv -q -p "$runtime/bin/python3" "$venv" \
        && VIRTUAL_ENV=$venv "$uv" pip install -q -e ".[dev]" \
        || { rm -rf "$venv"; fail "the test environment could not be created"; }
else
    # The environment outlives the checkout; point its editable install here.
    VIRTUAL_ENV=$venv "$uv" pip install -q --no-deps -e . || fail "vbot could not be installed"
fi

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

# As push.py on Windows: every core of the machine's test core pool, not the two
# `-n auto` asks for there.
workers=$("$python" -c 'from tests import cpu_pool; print(cpu_pool.pool_size())') \
    || fail "the test core pool size could not be read"
status=0
"$python" -m pytest -n "$workers" -rfE -q --no-header || status=$?
[ "$status" -eq 1 ] || exit "$status"
failed=$(last_failed)
[ -n "$failed" ] || exit 1
echo "linux tests: running the $(echo "$failed" | wc -l) failed tests again, alone"
rerun=0
"$python" -m pytest --last-failed --last-failed-no-failures none -n 0 -rfE -q --no-header || rerun=$?
still=$(last_failed)
while read -r test; do
    if [ "$rerun" -eq 0 ] || ! grep -qxF -- "$test" <<<"$still"; then echo "flaky: $test"; fi
done <<<"$failed"
exit "$rerun"
