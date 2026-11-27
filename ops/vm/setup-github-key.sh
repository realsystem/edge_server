#!/usr/bin/env bash
# Gives THIS machine (a VM) read-only access to ONE private GitHub repository through its own deploy key.
# Copy this file unchanged into any repo you deploy to a VM; nothing in it is specific to any one repository.
#
#   ./setup-github-key.sh OWNER/REPO [--clone [DIR]] [--write] [--no-prompt]
#   ./setup-github-key.sh                  (from inside an existing clone: the repo is read from its origin)
#
#   OWNER/REPO    for example acme/webapp
#   --clone [DIR] after the key works, clone the repo (into DIR, default: the repo name)
#   --fix-remote  after the key works, point this clone's origin at the deploy key
#   --write       only a reminder in the printed steps: tick "Allow write access" (default: read-only, which is
#                 what a deploy needs)
#   --no-prompt   do not wait for you to add the key on GitHub: print it and stop (re-run to test and clone)
#
# What it does (the usual manual recipe, automated):
#   1. creates ~/.ssh/deploy_<repo> (ed25519, no passphrase) if it does not exist
#   2. trusts github.com's host key only after checking its fingerprint against GitHub's published one
#   3. adds a "Host github-<repo>" block to ~/.ssh/config that uses only that key
#   4. prints the public key and what to click on GitHub, waits, then tests the login
#   5. optionally clones through the alias
# Safe to run again: existing keys and config are reused, never overwritten.
# A deploy key works for exactly one repository; run the script once per repo.

set -euo pipefail

# GitHub's published ed25519 host key fingerprint (https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
GITHUB_ED25519_FP='SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU'

say() { printf '\n==> %s\n' "$*"; }
ok() { printf '    ok: %s\n' "$*"; }
hint() { printf '    hint: %s\n' "$*"; }
die() {
  local msg="$1"
  shift
  printf '\nERROR: %s\n' "$msg" >&2
  for h in "$@"; do printf '    hint: %s\n' "$h" >&2; done
  exit 1
}

REPO=""
CLONE=0
CLONE_DIR=""
FIX_REMOTE=0
WRITE=0
PROMPT=1
OFFLINE=0 # for tests only: skip everything that talks to GitHub
while [ $# -gt 0 ]; do
  case "$1" in
    --clone)
      CLONE=1
      if [ $# -gt 1 ] && [[ "$2" != -* ]]; then CLONE_DIR="$2"; shift; fi
      shift
      ;;
    --fix-remote) FIX_REMOTE=1; shift ;;
    --write) WRITE=1; shift ;;
    --no-prompt) PROMPT=0; shift ;;
    --offline) OFFLINE=1; shift ;;
    -h | --help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) die "unknown option: $1" "run with --help" ;;
    *) [ -z "$REPO" ] || die "only one OWNER/REPO please"; REPO="$1"; shift ;;
  esac
done

for tool in ssh ssh-keygen ssh-keyscan git; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed" "sudo apt-get install -y openssh-client git"
done

# Which repository? From the argument, else from the origin of the clone we are in.
if [ -z "$REPO" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  url="$(git remote get-url origin 2>/dev/null || true)"
  REPO="$(printf '%s' "$url" | sed -E 's#^(https://github\.com/|git@[^:]+:|ssh://git@[^/]+/)##; s#\.git$##')"
fi
[ -n "$REPO" ] || die "which repository?" "pass it: ./setup-github-key.sh acme/webapp"
REPO="${REPO%.git}"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "'$REPO' is not OWNER/REPO" "example: acme/webapp"
NAME="${REPO#*/}"
SAFE="$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n-' '-')"
ALIAS="github-$SAFE"
KEY="$HOME/.ssh/deploy_$SAFE"
CONFIG="$HOME/.ssh/config"

say "Deploy key for $REPO"
install -d -m 700 "$HOME/.ssh"
if [ -f "$KEY" ]; then
  ok "key $KEY already exists, reusing it"
else
  ssh-keygen -q -t ed25519 -C "gcp-deploy-$SAFE" -f "$KEY" -N ""
  ok "created $KEY (ed25519, no passphrase: it can only read this one repo)"
fi
chmod 600 "$KEY"

if [ "$OFFLINE" -eq 0 ]; then
  say "Trusting github.com"
  scanned="$(ssh-keyscan -t ed25519 github.com 2>/dev/null || true)"
  [ -n "$scanned" ] || die "could not reach github.com on port 22" "check the VM's internet access and that outbound port 22 is allowed"
  fp="$(printf '%s\n' "$scanned" | ssh-keygen -lf - | awk '{print $2}')"
  if [ "$fp" != "$GITHUB_ED25519_FP" ]; then
    die "github.com's host key fingerprint is $fp, not the published $GITHUB_ED25519_FP" \
      "this can mean someone is intercepting the connection: do NOT continue on this network" \
      "if GitHub rotated its key, compare with https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints and update GITHUB_ED25519_FP"
  fi
  touch "$HOME/.ssh/known_hosts"
  if ssh-keygen -F github.com -f "$HOME/.ssh/known_hosts" >/dev/null 2>&1; then
    ok "github.com is already trusted"
  else
    printf '%s\n' "$scanned" >>"$HOME/.ssh/known_hosts"
    ok "added github.com (fingerprint matches GitHub's published one)"
  fi
fi

say "SSH alias $ALIAS"
touch "$CONFIG"
chmod 600 "$CONFIG"
if grep -q "^Host $ALIAS\$" "$CONFIG"; then
  ok "$CONFIG already has Host $ALIAS"
else
  {
    printf '\n# BEGIN %s (setup-github-key.sh)\n' "$ALIAS"
    printf 'Host %s\n\tHostName github.com\n\tUser git\n\tIdentityFile %s\n\tIdentitiesOnly yes\n' "$ALIAS" "$KEY"
    printf '# END %s\n' "$ALIAS"
  } >>"$CONFIG"
  ok "added Host $ALIAS to $CONFIG (uses only $KEY)"
fi

show_key() {
  access="leave 'Allow write access' UNTICKED (a deploy only needs to read)"
  [ "$WRITE" -eq 0 ] || access="tick 'Allow write access' (you asked for --write)"
  cat <<EOF

------------------------------------------------------------------
 ADD THIS PUBLIC KEY TO GITHUB (one time, in your browser):

 1. Open https://github.com/$REPO/settings/keys/new
    (or: the repo > Settings > Deploy keys > Add deploy key)
 2. Title: gcp-deploy-$SAFE ($(hostname 2>/dev/null | cut -d. -f1))
 3. Key: paste the single line below
 4. $access
 5. Add key

$(cat "$KEY.pub")
------------------------------------------------------------------
 (Faster, from a machine where 'gh' is logged in:
   gh repo deploy-key add <(echo '$(cat "$KEY.pub")') --repo $REPO --title gcp-deploy-$SAFE )
EOF
}

test_login() {
  local out
  out="$(ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "git@$ALIAS" 2>&1 || true)"
  if printf '%s' "$out" | grep -q "successfully authenticated"; then
    ok "$(printf '%s' "$out" | head -1)"
    return 0
  fi
  printf '    GitHub said: %s\n' "$(printf '%s' "$out" | head -2 | tr '\n' ' ')"
  return 1
}

if [ "$OFFLINE" -eq 1 ]; then
  show_key
  exit 0
fi

say "Testing the login"
if ! test_login; then
  show_key
  if [ "$PROMPT" -eq 0 ] || [ ! -t 0 ]; then
    hint "add the key, then run this script again to test and clone"
    exit 2
  fi
  while true; do
    read -r -p "Press Enter once the key is added on GitHub (or type q to stop): " answer
    [ "$answer" != "q" ] || { hint "run this script again when you are ready"; exit 2; }
    if test_login; then break; fi
    hint "'Permission denied' = the key is not added yet, or was added to a different repo (a deploy key works for one repo only)"
    hint "'Could not resolve hostname github-$SAFE' = the Host block is missing from $CONFIG"
  done
fi

REMOTE="git@$ALIAS:$REPO.git"
if [ "$CLONE" -eq 1 ]; then
  say "Cloning"
  dest="${CLONE_DIR:-$NAME}"
  if [ -e "$dest" ]; then
    ok "$dest already exists, not cloning again"
  else
    git clone "$REMOTE" "$dest"
    ok "cloned into $dest"
  fi
fi
if [ "$FIX_REMOTE" -eq 1 ]; then
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "--fix-remote must be run inside the clone"
  git remote set-url origin "$REMOTE"
  ok "origin now uses the deploy key: $REMOTE"
fi

cat <<EOF

Done. This VM can now read $REPO.
  clone:                git clone $REMOTE
  switch a clone here:  git remote set-url origin $REMOTE
  update a clone:       git pull
Always use the alias host ($ALIAS), not github.com, or git will not use this key.
Another repo on this VM: copy this script there and run it with that repo's name.
EOF
