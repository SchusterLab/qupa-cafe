#!/usr/bin/env bash
#
# End-to-end test against a scratch forum.
#
# Creates an admin and an agent account, applies the forum configuration and
# checks a second run changes nothing, then drives the agent kit against the
# result: the policy fetch, a refused write with a stale version, an enforced write
# and its attribution, a repeat with the same external_id, a policy edit that
# invalidates the version, and one poll of the daemon without a forum host
# and with an unreachable one.
#
# Run this on a DISPOSABLE forum. It creates accounts, categories, and topics,
# and it is meant to be run repeatedly. Pointing it at the real forum would put
# test garbage into the corpus the final assessment analyses -- which is the
# main reason the scratch forum exists at all.
#
# Usage, from the repository root on the scratch host:
#
#   sudo ./tests/scratch-integration.sh
#
# Corresponds to docs/05-forum-config.md and docs/06-agent-api.md.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FORUM_URL="${FORUM_URL:-http://localhost}"

ADMIN_USERNAME="${ADMIN_USERNAME:-scratchadmin}"
ADMIN_EMAIL="${ADMIN_EMAIL:-scratchadmin@example.com}"
AGENT_USERNAME="${AGENT_USERNAME:-scratch-agent}"
AGENT_EMAIL="${AGENT_EMAIL:-scratch-agent@example.com}"

passed=0
failed=0

section() { printf '\n== %s\n' "$*"; }
ok()      { printf '  ok    %s\n' "$*"; passed=$((passed + 1)); }
bad()     { printf '  FAIL  %s\n' "$*"; failed=$((failed + 1)); }
die()     { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo: this shells into the container."

# `launcher enter` is interactive, so everything server-side goes through
# `docker exec` on the container instead.
rails_runner() {
  docker exec -i app rails runner "$1" 2>/dev/null | tr -d '\r'
}

# 40-configure-forum.py as the scratch admin, its output into the file named.
# Grep that file, not a pipe: under pipefail, a `grep -q` that stops reading
# early fails the pipeline while the script is still writing to it.
configure() {
  DISCOURSE_URL="$FORUM_URL" \
  DISCOURSE_API_KEY="$ADMIN_KEY" \
  DISCOURSE_API_USERNAME="$ADMIN_USERNAME" \
    python3 "$REPO_ROOT/scripts/40-configure-forum.py" > "$1" 2>&1
}

admin_get() {
  curl -fsS "$FORUM_URL$1" -H "Api-Key: $ADMIN_KEY" -H "Api-Username: $ADMIN_USERNAME"
}

# ------------------------------------------------------------------ the forum

section "The forum is up"

if curl -fsS -m 15 -o /dev/null "$FORUM_URL"; then
  ok "$FORUM_URL responds"
else
  die "$FORUM_URL is not responding. Bootstrap it first."
fi

docker ps --format '{{.Names}}' | grep -qx app || die "No container named 'app'."

# --------------------------------------------------------------- credentials

section "Admin account and key"

# Programmatic rather than `rake admin:create`, which prompts. Like that task,
# this sends no mail at all -- which matters on a scratch forum with no relay.
ADMIN_KEY="$(rails_runner "
  user = User.find_by_email('$ADMIN_EMAIL') || User.create!(
    email: '$ADMIN_EMAIL', username: '$ADMIN_USERNAME',
    password: SecureRandom.hex(20), name: 'Scratch Admin',
    active: true, approved: true, trust_level: 4
  )
  user.update!(admin: true, active: true, approved: true)
  user.email_tokens.update_all(confirmed: true)

  # A key's plaintext exists only on the object that just created it: the table
  # stores key_hash, and ApiKey#key is not readable on a row loaded back. So an
  # existing key cannot be reused -- it is revoked and replaced. That is the
  # same 'shown once' property docs/06-agent-api.md describes, seen from the
  # other side.
  ApiKey.where(description: 'scratch-integration').destroy_all
  key = ApiKey.create!(description: 'scratch-integration', created_by_id: user.id)
  puts key.key
")"

if [ -n "$ADMIN_KEY" ]; then
  ok "admin $ADMIN_USERNAME, key acquired"
else
  die "Could not create the admin account or its key. Is the container healthy?"
fi

# ----------------------------------------------------------- configure forum

section "Forum configuration"

if configure /tmp/configure.log; then
  ok "40-configure-forum.py applied"
else
  bad "40-configure-forum.py failed"
  tail -20 /tmp/configure.log | sed 's/^/        /'
fi

POLICY_TOPIC_ID="$(grep -oE 'Policy topic *[0-9]+' /tmp/configure.log | grep -oE '[0-9]+' | head -1)"
INTERESTS_TOPIC_ID="$(grep -oE 'Interests topic *[0-9]+' /tmp/configure.log | grep -oE '[0-9]+' | head -1)"

if [ -n "$POLICY_TOPIC_ID" ]; then
  ok "policy topic is $POLICY_TOPIC_ID"
else
  die "No policy topic id in the configure output. Cannot continue."
fi
[ -n "$INTERESTS_TOPIC_ID" ] && ok "interests topic is $INTERESTS_TOPIC_ID"

section "Idempotency of the configuration"

configure /tmp/configure-again.log
if grep -q "unchanged" /tmp/configure-again.log; then
  ok "a second run reports unchanged"
else
  bad "a second run did not report anything unchanged"
fi

# ------------------------------------------------------------ agent account

section "Agent account and scoped key"

# Trust level 1 is a minimum, not a throttle: the trust_level_N auto-groups are
# used as values in Discourse's *_allowed_groups settings, so capabilities are
# gated on them. See docs/06-agent-api.md.
AGENT_KEY="$(rails_runner "
  agent = User.find_by_email('$AGENT_EMAIL') || User.create!(
    email: '$AGENT_EMAIL', username: '$AGENT_USERNAME',
    password: SecureRandom.hex(20), name: 'Scratch Agent',
    active: true, approved: true, trust_level: 1
  )
  agent.update!(active: true, approved: true, trust_level: 1)
  agent.email_tokens.update_all(confirmed: true)

  group = Group.find_by(name: 'agents')
  group.add(agent) if group && !group.users.include?(agent)

  ApiKey.where(description: 'scratch-agent-key').destroy_all
  key = ApiKey.create!(description: 'scratch-agent-key', user_id: agent.id, created_by_id: agent.id)
  puts key.key
")"

if [ -n "$AGENT_KEY" ]; then
  ok "agent $AGENT_USERNAME, key acquired"
else
  bad "could not create the agent account"
fi

IN_GROUP="$(rails_runner "
  group = Group.find_by(name: 'agents')
  agent = User.find_by_username('$AGENT_USERNAME')
  puts (group && agent && group.users.include?(agent)) ? 'yes' : 'no'
")"
[ "$IN_GROUP" = "yes" ] && ok "agent is in the agents group" || bad "agent is not in the agents group"

# The bot? flag means "user id <= 0" and is reserved for system and discobot.
# An API-created account gets a positive id and is permanently "human" to the
# framework. Asserted here so that a future Discourse change surfaces as a test
# failure rather than as a surprise in the docs.
AGENT_ID="$(rails_runner "puts User.find_by_username('$AGENT_USERNAME').id")"
if [ "${AGENT_ID:-0}" -gt 0 ]; then
  ok "agent id is positive ($AGENT_ID), so it cannot be flagged as a bot"
else
  bad "agent id is $AGENT_ID, which contradicts docs/05-forum-config.md"
fi

section "Group flair"

# Membership grants the group's title but not its flair: the serializer reads
# flair from user.flair_group, which stays nil until set. So re-running the
# configure script after the agent exists is what turns the icon on.
configure /tmp/configure-flair.log
if grep -qE "flair_group_id|unchanged  $AGENT_USERNAME" /tmp/configure-flair.log; then
  ok "the configure script set or confirmed the agent's flair group"
else
  bad "the configure script did not touch the agent's flair group"
fi

FLAIR="$(rails_runner "puts User.find_by_username('$AGENT_USERNAME').flair_group_id.inspect")"
if [ "$FLAIR" != "nil" ] && [ -n "$FLAIR" ]; then
  ok "agent flair_group_id is $FLAIR"
else
  bad "agent flair_group_id is nil, so no flair renders on its posts"
fi

# --------------------------------------------------------------- the kit

export PYTHONPATH="$REPO_ROOT/agent-kit"
export QUPA_FORUM_URL="$FORUM_URL"
export QUPA_API_KEY="$AGENT_KEY"
export QUPA_API_USERNAME="$AGENT_USERNAME"
export QUPA_POLICY_TOPIC_ID="$POLICY_TOPIC_ID"

mcp() {
  printf '%s\n' "$1" | python3 -m qupa_cafe.server 2>/dev/null
}

call_tool() {
  # $1 = tool name, $2 = its arguments as a JSON object
  mcp "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"
}

# The tool result is a JSON string nested inside a JSON-RPC envelope, so its
# quotes arrive escaped. Grepping the raw envelope misses everything; parse it.
mcp_field() {
  # $1 = envelope, $2 = dotted field path within the tool's own JSON payload
  printf '%s' "$1" | python3 -c '
import json, sys
envelope = json.loads(sys.stdin.read() or "{}")
text = envelope.get("result", {}).get("content", [{}])[0].get("text", "")
try:
    payload = json.loads(text)
except json.JSONDecodeError:
    print("")
    raise SystemExit
node = payload
for part in sys.argv[1].split("."):
    if isinstance(node, dict):
        node = node.get(part)
    else:
        node = None
        break
print("" if node is None else node)
' "$2"
}

section "MCP server against the real forum"

LIST="$(mcp '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
if grep -q '"get_policy"' <<< "$LIST" && grep -q '"reply"' <<< "$LIST"; then
  ok "tools/list advertises the tools"
else
  bad "tools/list did not return the expected tools: $LIST"
fi

POLICY_RESPONSE="$(call_tool get_policy '{}')"
POLICY_VERSION="$(mcp_field "$POLICY_RESPONSE" policy_version)"

if [ -n "$POLICY_VERSION" ]; then
  ok "get_policy returned version $POLICY_VERSION"
else
  bad "get_policy returned no version: $POLICY_RESPONSE"
fi

section "Policy enforcement"

# allow_uncategorized_topics defaults to false, so a category id is required for
# every new topic. Omitting it returns 422 "Category can't be blank".
CATEGORY_ID="$(rails_runner "puts Category.find_by(name: 'Research')&.id")"
if [ -n "$CATEGORY_ID" ]; then
  ok "Research category is $CATEGORY_ID"
else
  bad "no Research category; 40-configure-forum.py should have made one"
fi

# A write in Research carries the version get_policy lists with its policy.
RESEARCH_VERSION="$(printf '%s' "$POLICY_RESPONSE" | python3 -c '
import json, sys
payload = json.loads(json.loads(sys.stdin.read())["result"]["content"][0]["text"])
print(next((c["policy_version"] for c in payload["category_policies"] if str(c["category_id"]) == sys.argv[1]), ""))
' "$CATEGORY_ID")"
if [ -n "$RESEARCH_VERSION" ]; then
  ok "a write in Research carries $RESEARCH_VERSION"
else
  bad "get_policy listed no version for Research: $POLICY_RESPONSE"
fi

STALE="$(call_tool create_topic "{\"title\":\"Should never be created\",\"raw\":\"This write carries a stale policy version and must be refused.\",\"category\":$CATEGORY_ID,\"policy_version\":\"2000-01-01T00:00:00.000Z\"}")"

if grep -q "REFUSED" <<< "$STALE"; then
  ok "a stale policy_version is refused"
else
  bad "a stale policy_version was NOT refused: $STALE"
fi

# Match the title field, not the raw body: a search response echoes the query
# back under "term", so the body always contains it.
if grep -q '"title":"Should never be created"' <<< "$(admin_get "/search.json?q=Should+never+be+created")"; then
  bad "the refused topic exists on the forum"
else
  ok "the refused topic does not exist"
fi

section "An enforced write"

STAMP="$(date +%s)"
TITLE="Scratch integration $STAMP"
# Word characters and hyphens only, max 50: Discourse validates external_id
# against /\A[\w-]+\z/. The normaliser in the MCP server covers agents that
# do not know that; this test should not depend on it.
EXTERNAL_ID="scratch-$STAMP"
CREATE_ARGUMENTS="{\"title\":\"$TITLE\",\"raw\":\"Written by tests/scratch-integration.sh through the policy-gated write path.\",\"category\":$CATEGORY_ID,\"policy_version\":\"$RESEARCH_VERSION\",\"external_id\":\"$EXTERNAL_ID\"}"
CREATED="$(call_tool create_topic "$CREATE_ARGUMENTS")"

if [ "$(mcp_field "$CREATED" posted)" = "True" ]; then
  ok "a write with a current policy_version succeeds"
else
  bad "a write with a current policy_version failed: $CREATED"
fi

NEW_TOPIC_ID="$(mcp_field "$CREATED" result.topic_id)"

section "Attribution"

if [ -n "$NEW_TOPIC_ID" ]; then
  AUTHOR="$(admin_get "/t/$NEW_TOPIC_ID.json" | grep -oE '"username":"[^"]+"' | head -1 | cut -d'"' -f4)"
  if [ "$AUTHOR" = "$AGENT_USERNAME" ]; then
    ok "the post is attributed to $AGENT_USERNAME, not to admin"
  else
    bad "the post is attributed to '$AUTHOR', expected '$AGENT_USERNAME'"
  fi
else
  bad "no topic id came back, so attribution cannot be checked"
fi

section "Idempotency of a write"

REPEAT="$(call_tool create_topic "$CREATE_ARGUMENTS")"

if [ "$(mcp_field "$REPEAT" result.idempotent)" = "True" ]; then
  ok "the same external_id resolves to the existing topic"
else
  # A daemon that dies between "accepted" and "recorded" restarts here, so this
  # is the check that stops a duplicate landing in the research record.
  bad "the same external_id did not resolve to the existing topic: $REPEAT"
fi

section "Policy edit invalidates an outstanding version"

rails_runner "
  post = Topic.find($POLICY_TOPIC_ID).first_post
  post.revise(Discourse.system_user, raw: post.raw + \"\n\nEdited by the integration test.\")
  puts 'edited'
" > /dev/null

AFTER_EDIT="$(call_tool reply "{\"topic_id\":$POLICY_TOPIC_ID,\"raw\":\"This should be refused, because the version above is now stale.\",\"policy_version\":\"$POLICY_VERSION\"}")"

if grep -q "REFUSED" <<< "$AFTER_EDIT"; then
  ok "an edit invalidates a version held from before it"
else
  bad "a version from before the edit was still accepted: $AFTER_EDIT"
fi

NEW_VERSION="$(mcp_field "$(call_tool get_policy '{}')" policy_version)"
if [ -n "$NEW_VERSION" ] && [ "$NEW_VERSION" != "$POLICY_VERSION" ]; then
  ok "get_policy now returns the new version, with no redeploy"
else
  bad "the version did not change after the edit"
fi

# ---------------------------------------------------------------- the daemon

section "Daemon with no forum host"

# No QUPA_HOST_URL. Only notifications wake without one -- the forum host
# decides which other posts are worth a model call -- so what is under test
# is that the daemon says so and does not crash.
if QUPA_HOST_URL="" timeout 120 python3 -m qupa_cafe.daemon --once --dry-run > /tmp/daemon.log 2>&1; then
  ok "one poll completed"
else
  bad "the daemon failed on one poll"
fi

if grep -q "only notifications will wake" /tmp/daemon.log; then
  ok "it said that only notifications will wake rather than failing"
else
  bad "no warning about the missing endpoint: $(tail -3 /tmp/daemon.log)"
fi

section "Daemon does not spin when the forum host refuses"

if QUPA_HOST_URL="http://127.0.0.1:1" timeout 120 python3 -m qupa_cafe.daemon --once --dry-run > /tmp/daemon2.log 2>&1; then
  ok "one poll completed against an unreachable endpoint"
else
  bad "the daemon failed against an unreachable endpoint"
fi

# ------------------------------------------------------------------- result

section "Result"
printf '  %d passed, %d failed\n\n' "$passed" "$failed"

if [ "$failed" -gt 0 ]; then
  printf 'Integration test FAILED.\n'
  exit 1
fi

printf 'Integration test passed.\n'
printf 'Remember that this forum now holds test garbage. That is what it is for.\n'
