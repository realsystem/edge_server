#!/usr/bin/env bash
# Unit tests for shell scripts
# Usage: ./test-scripts.sh

set -uo pipefail
cd "$(dirname "$0")/.."

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASSED=0
FAILED=0
SKIPPED=0

pass() {
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    PASSED=$((PASSED + 1))
}

fail() {
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    [ -n "${2:-}" ] && echo -e "         $2"
    FAILED=$((FAILED + 1))
}

skip() {
    echo -e "  ${YELLOW}○ SKIP${NC}: $1"
    SKIPPED=$((SKIPPED + 1))
}

section() {
    echo ""
    echo "━━━ $1 ━━━"
}

#-----------------------------------------------------------------------------
# Test: Script syntax validation (shellcheck + bash -n)
#-----------------------------------------------------------------------------

section "Syntax Validation"

for script in bootstrap.sh initial-setup.sh deploy-edge-server.sh deploy-security.sh secrets.sh; do
    if [ -f "$script" ]; then
        if bash -n "$script" 2>/dev/null; then
            pass "$script syntax OK"
        else
            fail "$script has syntax errors"
        fi
    else
        skip "$script not found"
    fi
done

for script in testing/test-runner.sh testing/test-runner-full.sh testing/test-lite.sh; do
    if [ -f "$script" ]; then
        if bash -n "$script" 2>/dev/null; then
            pass "$script syntax OK"
        else
            fail "$script has syntax errors"
        fi
    else
        skip "$script not found"
    fi
done

#-----------------------------------------------------------------------------
# Test: ShellCheck linting (if available)
#-----------------------------------------------------------------------------

section "ShellCheck Linting"

if command -v shellcheck >/dev/null 2>&1; then
    for script in bootstrap.sh secrets.sh testing/test-runner.sh testing/test-runner-full.sh; do
        if [ -f "$script" ]; then
            # SC1090: Can't follow non-constant source
            # SC1091: Not following sourced file
            # SC2034: Variable appears unused (often used in sourced context)
            # SC2016: Expressions don't expand in single quotes (intentional for $SYS)
            # SC2329: Function never invoked (cleanup is called via trap)
            if shellcheck -e SC1090,SC1091,SC2034,SC2016,SC2329 "$script" 2>/dev/null; then
                pass "$script shellcheck OK"
            else
                fail "$script has shellcheck warnings"
            fi
        fi
    done
else
    skip "shellcheck not installed (brew install shellcheck)"
fi

#-----------------------------------------------------------------------------
# Test: bootstrap.sh
#-----------------------------------------------------------------------------

section "bootstrap.sh"

# Test: --help works
output=$(./bootstrap.sh --help 2>&1 || true)
if echo "$output" | grep -q "Usage:"; then
    pass "--help displays usage"
else
    fail "--help does not display usage"
fi

# Test: Missing target IP
output=$(./bootstrap.sh 2>&1 || true)
if echo "$output" | grep -q "Target IP required"; then
    pass "Exits with error when no target provided"
else
    fail "Does not error on missing target" "Got: $output"
fi

# Test: Invalid --deploy type
output=$(./bootstrap.sh --deploy invalid 192.168.1.1 2>&1 || true)
if echo "$output" | grep -q "must be base, security, or full"; then
    pass "Rejects invalid --deploy type"
else
    fail "Accepts invalid --deploy type" "Got: $output"
fi

# Test: --dry-run flag recognized (use --auto to avoid prompts)
output=$(timeout 5 ./bootstrap.sh --dry-run --auto --skip-init 192.0.2.1 2>&1 || true)
if echo "$output" | grep -q "DRY RUN"; then
    pass "--dry-run mode recognized"
else
    fail "--dry-run mode not recognized" "Got: $output"
fi

# Test: Missing secrets file
output=$(./bootstrap.sh --secrets-file /nonexistent/file 192.168.1.1 2>&1 || true)
if echo "$output" | grep -q "Secrets file not found"; then
    pass "Errors on missing secrets file"
else
    fail "Does not error on missing secrets file" "Got: $output"
fi

#-----------------------------------------------------------------------------
# Test: secrets.sh
#-----------------------------------------------------------------------------

section "secrets.sh"

# Create temp directory for secrets tests
SECRETS_TEST_DIR=$(mktemp -d)
trap 'rm -rf "$SECRETS_TEST_DIR"' EXIT

# Set password via environment to avoid interactive prompts
export SECRETS_PASSWORD="testpassword123"

# Test: init creates directory (simulate password input)
printf '%s\n%s\n' "$SECRETS_PASSWORD" "$SECRETS_PASSWORD" | HOME="$SECRETS_TEST_DIR" ./secrets.sh init >/dev/null 2>&1 || true
if [ -d "$SECRETS_TEST_DIR/.edge-server-secrets" ]; then
    pass "init creates secrets directory"
else
    fail "init does not create secrets directory"
fi

# Test: set and get
if HOME="$SECRETS_TEST_DIR" ./secrets.sh set TEST_KEY "test_value" >/dev/null 2>&1; then
    result=$(HOME="$SECRETS_TEST_DIR" ./secrets.sh get TEST_KEY 2>/dev/null || echo "")
    if [ "$result" = "test_value" ]; then
        pass "set/get roundtrip works"
    else
        fail "set/get roundtrip failed" "Expected 'test_value', got '$result'"
    fi
else
    fail "secrets.sh set command failed"
fi

# Test: list shows keys
if HOME="$SECRETS_TEST_DIR" ./secrets.sh list 2>/dev/null | grep -q "TEST_KEY"; then
    pass "list shows stored keys"
else
    fail "list does not show stored keys"
fi

# Test: export format
export_output=$(HOME="$SECRETS_TEST_DIR" ./secrets.sh export 2>/dev/null || echo "")
if echo "$export_output" | grep -q "export TEST_KEY="; then
    pass "export produces valid shell export"
else
    fail "export does not produce valid shell export"
fi

# Test: get nonexistent key
result=$(HOME="$SECRETS_TEST_DIR" ./secrets.sh get NONEXISTENT 2>/dev/null || echo "")
if [ -z "$result" ]; then
    pass "get nonexistent key returns empty"
else
    fail "get nonexistent key returns non-empty"
fi

unset SECRETS_PASSWORD

#-----------------------------------------------------------------------------
# Test: initial-setup.sh
#-----------------------------------------------------------------------------

section "initial-setup.sh"

# Test: Script requires root
if ./initial-setup.sh 2>&1 | grep -qi "root\|sudo\|permission"; then
    pass "Checks for root/sudo"
else
    skip "Cannot verify root check without running as non-root"
fi

# Test: Has expected configuration sections
if grep -q "static IP\|Docker\|netplan" initial-setup.sh 2>/dev/null; then
    pass "Contains expected configuration logic"
else
    fail "Missing expected configuration logic"
fi

#-----------------------------------------------------------------------------
# Test: deploy-edge-server.sh
#-----------------------------------------------------------------------------

section "deploy-edge-server.sh"

# Test: Script requires root
if ./deploy-edge-server.sh 2>&1 | grep -qi "root\|sudo\|permission"; then
    pass "Checks for root/sudo"
else
    skip "Cannot verify root check without running as non-root"
fi

# Test: Has docker-compose generation
if grep -q "docker.compose\|docker compose" deploy-edge-server.sh 2>/dev/null; then
    pass "Contains docker compose logic"
else
    fail "Missing docker compose logic"
fi

#-----------------------------------------------------------------------------
# Test: deploy-security.sh
#-----------------------------------------------------------------------------

section "deploy-security.sh"

# Test: Script requires root
if ./deploy-security.sh 2>&1 | grep -qi "root\|sudo\|permission"; then
    pass "Checks for root/sudo"
else
    skip "Cannot verify root check without running as non-root"
fi

# Test: Has Frigate config
if grep -q "frigate\|FRIGATE" deploy-security.sh 2>/dev/null; then
    pass "Contains Frigate configuration"
else
    fail "Missing Frigate configuration"
fi

#-----------------------------------------------------------------------------
# Test: test-runner.sh
#-----------------------------------------------------------------------------

section "test-runner.sh"

# Test: Has pass/fail functions
if grep -q "^pass()" testing/test-runner.sh && grep -q "^fail()" testing/test-runner.sh; then
    pass "Has pass/fail test functions"
else
    fail "Missing pass/fail functions"
fi

# Test: Has cleanup trap
if grep -q "trap.*cleanup\|trap.*EXIT" testing/test-runner.sh; then
    pass "Has cleanup trap"
else
    fail "Missing cleanup trap"
fi

#-----------------------------------------------------------------------------
# Test: Docker Compose files validation
#-----------------------------------------------------------------------------

section "Docker Compose Files"

if command -v docker >/dev/null 2>&1; then
    for compose_file in testing/docker-compose.lite.yml testing/docker-compose.mac.yml testing/docker-compose.test-harness.yml; do
        if [ -f "$compose_file" ]; then
            if docker compose -f "$compose_file" config >/dev/null 2>&1; then
                pass "$compose_file valid"
            else
                fail "$compose_file invalid"
            fi
        else
            skip "$compose_file not found"
        fi
    done
else
    skip "Docker not available for compose validation"
fi

#-----------------------------------------------------------------------------
# Test: Makefile targets
#-----------------------------------------------------------------------------

section "Makefile"

# Test: Has expected targets
for target in test test-full start stop clean help; do
    if grep -q "^${target}:" Makefile 2>/dev/null; then
        pass "Has target: $target"
    else
        fail "Missing target: $target"
    fi
done

#-----------------------------------------------------------------------------
# Results
#-----------------------------------------------------------------------------

section "Results"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "  ${GREEN}Passed${NC}:  %d\n" "$PASSED"
printf "  ${RED}Failed${NC}:  %d\n" "$FAILED"
printf "  ${YELLOW}Skipped${NC}: %d\n" "$SKIPPED"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ "$FAILED" -gt 0 ]; then
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
