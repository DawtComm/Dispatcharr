#!/bin/bash
#
# Unit-level test for the environment forwarding block in docker/entrypoint.sh.
#
# uWSGI (and the Celery workers it attaches) are started through `su -`, which
# discards the container environment. entrypoint.sh therefore writes a fixed list
# of variables to /etc/profile.d/dispatcharr.sh and /etc/environment, and only
# those reach the workers. This test runs that exact block against temporary files
# and checks that a variable which application code reads from the environment
# (XC_PROFILE_REFRESH_DELAY, see dispatcharr/settings.py) is forwarded.
#
# No Docker required.
#
# Usage:
#   cd <repo_root>
#   bash docker/tests/test-env-forwarding.sh

set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="$REPO_ROOT/docker/entrypoint.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
check() { # description, condition-exit-code
    if [ "$2" -eq 0 ]; then echo "PASS: $1"; else echo "FAIL: $1"; fail=1; fi
}

# Extract the forwarding block: from "variables=(" through the chmod that ends it,
# with the system paths redirected into the temp dir.
block="$(sed -n '/^variables=(/,/^chmod +x \/etc\/profile.d\/dispatcharr.sh/p' "$ENTRYPOINT")"
if [ -z "$block" ]; then
    echo "FAIL: could not find the variables block in docker/entrypoint.sh"
    exit 1
fi
block="${block//\/etc\/profile.d\/dispatcharr.sh/$TMP/dispatcharr.sh}"
block="${block//\/etc\/environment/$TMP/environment}"
: > "$TMP/environment"

run_block() { # runs the block in a clean subshell with the given env assignments
    ( unset XC_PROFILE_REFRESH_DELAY
      export PATH VIRTUAL_ENV=/venv "$@"
      eval "$block" ) >/dev/null 2>&1
}

# 1. A set XC_PROFILE_REFRESH_DELAY is forwarded to both files.
run_block XC_PROFILE_REFRESH_DELAY=45
grep -q "^export XC_PROFILE_REFRESH_DELAY='45'$" "$TMP/dispatcharr.sh"
check "XC_PROFILE_REFRESH_DELAY is written to profile.d" $?
grep -q "^XC_PROFILE_REFRESH_DELAY='45'$" "$TMP/environment"
check "XC_PROFILE_REFRESH_DELAY is written to /etc/environment" $?

# 2. A login shell (what `su -` gives uWSGI/Celery) sees the value.
seen="$(env -i bash -c ". '$TMP/dispatcharr.sh'; echo \"\${XC_PROFILE_REFRESH_DELAY:-unset}\"")"
[ "$seen" = "45" ]
check "a login shell sourcing profile.d sees XC_PROFILE_REFRESH_DELAY=45 (got: $seen)" $?

# 3. When unset it is not written (the setting keeps its code default).
: > "$TMP/environment"
run_block
! grep -q "XC_PROFILE_REFRESH_DELAY" "$TMP/dispatcharr.sh"
check "an unset XC_PROFILE_REFRESH_DELAY is not written" $?

exit $fail
