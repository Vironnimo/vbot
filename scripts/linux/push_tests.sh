#!/usr/bin/env bash
# The complete backend suite on Linux for scripts/push.py, run inside WSL on the
# checked commit, which push.py has unpacked into "$1".
#
# As in CI: the one supported Python version with the dev extra installed
# editable, the pinned search engine and the token estimation encodings
# provisioned first, and failed tests run once more alone; those that pass then
# are printed as "flaky: <node id>" and do not fail the run. Everything lasting
# lives in ~/.cache/vbot-push: uv (the version pyproject.toml pins), one
# environment per pyproject.toml and Python version, the search engine and the
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
python_version=$(sed -n 's/^PYTHON_VERSION = "\(.*\)"$/\1/p' scripts/package_build.py)
[ -n "$uv_version" ] && [ -n "$python_version" ] || fail "uv or Python version pin not found"

uv=$cache/uv-$uv_version/bin/uv
if [ ! -x "$uv" ]; then
    rm -rf "$cache/uv-$uv_version"
    python3 -m venv "$cache/uv-$uv_version" \
        && "$cache/uv-$uv_version/bin/pip" install -q "uv==$uv_version" \
        || fail "uv $uv_version could not be installed (needs python3 with venv)"
fi

key=$( (cat pyproject.toml; echo "$python_version") | sha256sum | cut -c1-16)
venv=$cache/venv-$key
python=$venv/bin/python
if [ ! -x "$python" ]; then
    find "$cache" -maxdepth 1 -name 'venv-*' -exec rm -rf {} +
    "$uv" venv -q -p "$python_version" "$venv" \
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

status=0
"$python" -m pytest -rfE -q --no-header || status=$?
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
