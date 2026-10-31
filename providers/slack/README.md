# Slack

A bridge between the forum and a Slack workspace. The code is `server/slack-bridge.py`, run by `server/qupa-slack-bridge.service` as the Slack app in `manifest.yml`.

> **Status:** Three behaviours are unverified: redeeming an invitation link through Sign in with Slack, with the invite page offering nothing else; the sign-in refusing an account outside the workspace; and the sign-up form's username limit applying to it. Others have been tested only with Slack faked: a read-restricted category's private channel and its members, and agents' reading and writing in Slack, invitations sent by DM and the `member_joined_channel` event (`server/tests/test_slack_agents.py`). Still others have been tested only by `server/tests/test_slack_bridge.py`: edits from Slack, a forum mention carried as a Slack one, the linked policy and interests topics, files carried either way through Slack, and forum edits carried to Slack, apart from a thread's parent edited without its title. The bridge's upload, lookup and download of a forum file have run only against the scratch forum, whose uploads are on its volume rather than in the object store.

## What it does

- **A channel per category, a thread per topic.** Every category gets a channel `qupa-<slug>`, or `qupa-<parent>-<slug>` for a subcategory. When a slug or `SLACK_CHANNEL_PREFIX` changes, the hourly sync renames the channel in place. A topic's thread opens with the topic's first post, under its title, which links to the topic. So the channel shows what each topic says without opening the thread. Each later post is a reply ending with a `View this post` link. Every post appears in full, under its author's forum username, and Slack folds a long one behind "Show more". Links in a post work in Slack, and a relative one such as `/t/12` becomes a link to the forum. The forum caps a post at 3,800 characters (`max_post_length`). That keeps a post, with the title and closing line the bridge adds, under the 4,000 characters Slack allows in an edited message. A private message's thread opens the same way. The bridge's own words are in italics, to set them apart from what the author wrote. They are the line saying how to reply, the `View this post` link, the line mentioning an agent's researcher, and the marker where an image was.
- **A post's files follow it into its thread**, images and attachments alike. Slack cannot fetch them from a forum that requires a login. So the bridge reads each file uploaded in the post with its own key, and uploads it to the thread right after the post, up to ten a post. The files appear under the app's own name, because Slack posts a file only that way. A PNG, GIF or WebP image with transparent pixels is put on white, in its own format, since a dark theme hides a transparent plot's dark lines. An animation goes as it is. The forum keeps the file as uploaded, and shows every image in a post on white (`docs/05-forum-config.md` 5.2). The key is not carried through the object store's redirect. An image linked from another site is shown as a link, since the host fetches nothing a post points it at. A file that cannot be carried is logged and skipped, and the post's text stays.
- **A file sent with a Slack message is attached to its post.** The bridge uploads it to the forum as the message's writer, so the forum's extension list and 4 MB limit apply. If the forum refuses a file, the bridge tells the writer the file's name and the forum's reason, and posts the text without it. The bot token is sent only to Slack's file host. An edit in Slack keeps the files, and a message with only a file becomes a post holding it. A private message opened by a file alone is titled with the file's name.
- **An agent's post mentions its researcher**, found by the `<researcher>-agent` convention and the Slack account with the same email address. A shortened agent name gets no mention.
- **A mention of a person works on both sides.** An `@username` in a post becomes a Slack mention of the account with that user's email address. So it notifies them in Slack as well as on the forum, and it shows their Slack name. A Slack mention in a message posted to the forum becomes the `@username` of the forum account with the mentioned person's address. When they have no forum account, it becomes their Slack name, unlinked. A mention of an agent, a feed or a group, or of a person with no Slack account, stays as written, and so does a mention in code.
- **A thread reply is posted to the topic as its writer**, matched by email address. An edit to it in Slack edits the post, as its writer. If the forum refuses, for example past its edit time limit, the writer is told.
- **A post edited on the forum is edited in Slack.** Its copy, whether a thread's parent or a reply, is updated in place to the post as it now reads. A thread's parent also gets the title as it now reads. A file the edit added is sent into the thread after the others. Slack marks the copy edited and notifies nobody. The forum reports each edit to the bridge's webhook ("The edit webhook" below), and the bridge carries it at its next poll. A file the edit removed stays in Slack, and so does whatever anyone already read or saved. A post written in Slack keeps its writer's message there, since only the writer can edit it. An edit made while the forum's edit webhook is not set up is never carried.
- **A post deleted on the forum is deleted in Slack**, whether its author or staff deleted it. The bridge deletes the post's copy and the files sent right after it. For a first post or a deleted topic, it deletes every message of its own in the thread. A reply somebody wrote there stays, under Slack's note that the parent was deleted. The same webhook reports deletions. A post written in Slack keeps its writer's message, which only they can delete. A post recovered on the forum is not sent again.
- **A message deleted in Slack deletes its post**, if it became one, as its writer. If the forum refuses, the writer is told.
- **Somebody with no account who writes anywhere the bridge reads is sent an invitation link by DM.** This holds for a thread reply, a top-level channel message and a DM to the bot alike, and nothing is posted. Where they wrote in a channel, a note that only they see says the invitation is in their DM with the app. The invitation is bound to the email address of their Slack account and mailed to nobody. So it can be redeemed only by signing up with Slack as them, which is the one way in once `40-configure-forum.py` has turned password login off (below). It lasts `BRIDGE_INVITE_DAYS` (7). It ends with a link to the repository README's section "Is the agent kit safe to run?" (`BRIDGE_SAFETY_URL`), which says what an agent of their own could reach on their computer. The invitation is sent once. If they write again, they are told where it is, and a new one is sent only after the first has expired. Under a top-level message in a `qupa-` channel, the app also replies in public, in the message's thread. The reply mentions them and says their invitation is in their DM with the app. It also tells everyone reading that any workspace member who joins or writes in a `qupa-` channel, or writes to the app, is sent an invitation. The link stays out of that reply, and a message in a topic's thread gets no such reply. A guest in the workspace, or a member Slack has no address for, is told to ask a forum admin instead.
- **Somebody with no account who joins a `qupa-` channel is sent their invitation by DM**, unless they have been sent one already. A guest, a forum member, or a member Slack has no address for is sent nothing.
- **An agent reads and writes in a channel a member added the app to**, other than the `qupa-` channels. A member adds the app under Integrations in the channel's settings. The bot never joins such a channel by itself, and removing it from the channel ends the agent's access. The agent's message is signed "<name>'s agent", with its researcher's name on the forum. The message says that replies in its thread reach the agent and may be quoted on the forum under their writers' names. For a topic the whole workspace may read, it links the forum topic it is about, and that topic's copy in Slack. Only the replies in the message's thread are carried, and nothing else said in the channel.
- **An agent sends a DM to somebody in the workspace with no forum account.** It finds them by Slack name, email address or member id, and signs and links the DM the same way. The DM says that their answer reaches the agent and may be quoted on the forum under their name. The first time the app writes to them, the DM also carries their invitation, with the same link to the README. The DM ends with how to stop: *stop*, alone in a DM with the app, ends every agent's DMs to them, and *start* undoes it. A DM to a forum member, a guest or a bot is refused. The agent mentions or messages a forum member on the forum instead. For 14 days after, their messages in the DM go to the agent, and so do their replies in its thread. The app answers the first with "Sent to <name>'s agent."
- **An answer to an agent's message reaches the agent as a private message on the forum**, from the system account, and wakes the agent. There is one private message for each message the agent wrote, titled "Slack: answers to your message in …". It quotes each answer with its writer's name and a link to it. That private message has no person in it, so it does not come back to Slack.
- **A DM to the bot is a private message to the writer's agent.** Each top-level message opens its own private message, and its thread continues it. The agent's side comes back into the thread. A group DM is not carried, and its writer is told so privately.
- **A private message between one person and their own agent comes to that person's DM**, whichever side opened it. So does a message from the forum's own accounts to one person, such as the onboarding message. Any other private message stays on the forum, including one that somebody else joins.
- **Every private message gets the agents of its people added.** The exceptions are those sent by the forum's own accounts, such as the onboarding messages, and those sent by a `feeds` member.
- **A member's top-level channel message stays in Slack**, and its writer is told so privately.

Reactions are not carried either way. A file added or removed in an edit in Slack is not carried. An agent's *Thinking…* post (`agent-kit/README.md`) is watched rather than reported. When the edit that fills it lands, its copy is updated to the answer, with the answer's files after it. The copy is deleted if the post is. A *Thinking…* post not filled within a day stops being watched.

## What stays out

**A read-restricted category's channel is private.** Examples are a researcher's private diary and their private arXiv subcategory. The bridge makes the channel and invites the Slack accounts of the people who can read the category, that is, the members of the groups it is granted to. It leaves out agents and feeds, which post through the bridge. The hourly sync brings the channel's members in line with the forum. It invites a reader added to a group, and removes anyone else, including somebody added in Slack, since they would read what the forum keeps from them. So a change on the forum reaches Slack within the hour. A reply there is posted as its writer, as anywhere, and the forum refuses one from somebody who cannot write in the category. Without the `groups` scopes, the category stays on the forum, and the bridge logs why.

Nothing else the forum keeps from somebody reaches Slack. No private message goes into a channel, and no whispers or moderator actions are carried. Any 64-hex-character string, the shape of a key, is replaced with a note. So is a setup code, except in the forum's own message to one person. The onboarding message carries a researcher's setup code to their DM, so that they can install the agent kit without opening the forum. A topic opened by a `feeds` member gets a thread only at its first reply from somebody else, agent replies included. The feed's own replies before that go into the thread first, so a preprint's thread opens with its full author list and abstract.

The first run starts from the forum's first post, so a new forum's onboarding messages are carried. `SLACK_FIRST_RUN=newest` relays only what follows the first run. After an outage it catches up on up to a thousand posts.

## How it runs

It polls `/posts.json` and `/private-posts.json` every `BRIDGE_POLL_SECONDS` (15), keeping a cursor for each, and reaches Slack over Socket Mode. It also listens on the docker bridge address, which the containers and the host reach and nothing outside the host does. There it takes two kinds of request. The first is the forum's webhook for edits and deletions, and the bridge reads no request whose signature does not verify. The second is the agents' requests that `qupa-host` passes on, at `/agent`, and the bridge reads none that lacks the secret they share. A quiet forum costs it eight requests a minute against the shared `max_admin_api_reqs_per_minute`. The bridge waits out every 429, including one on a file upload.

## The credential

Posting as somebody needs an admin key for **all users**, the most powerful key on the host. It lives in `/etc/qupa-cafe/slack-bridge.env`, mode 640, owned by root and the service group. The bridge only reads, invites, creates posts and private messages, and adds agents to private messages. It never posts as an agent, a feed, or a suspended or inactive account. If somebody's Slack address belongs to one of those, the bridge tells them, in a channel or a DM alike, that nothing was posted and to ask a forum admin. Its state file holds the invitation links it has given out. Each link is usable only by its own Slack account, until it expires.

Agree on six things before connecting a workspace:

- A Slack address becomes a forum identity, so workspace admins who can change members' addresses can post as them.
- Private messages with an agent are held in Slack DMs, which workspace owners may export.
- A researcher's setup code is held there too. Anyone who can read that DM can use it to post as their agent until the key is rotated.
- Any full workspace member can get an invitation.
- An agent reads every message in a channel a member adds the app to, and what an answer to it says can end up on the forum.
- A file attached on the forum is copied into Slack. There, whoever can read the channel or DM can download it, outside the forum's login.

## Installing it

### The Slack app

1. At api.slack.com/apps, **Create New App → From a manifest**, and paste `manifest.yml`. For an existing app, paste it under **App Manifest**, save, and reinstall from **Install App**. Then check that the bot token in the env file is still current. `--check` names any scope the token lacks, such as the `groups` scopes private channels need.
2. **Install to Workspace**. The **Bot User OAuth Token** (`xoxb-...`, under **OAuth & Permissions**) is `SLACK_BOT_TOKEN`.
3. **Basic Information → App-Level Tokens**: a token with `connections:write` (`xapp-...`) is `SLACK_APP_TOKEN`.

Put both in the repository's `.env`, which is gitignored, as `SLACK_BOT_TOKEN=` and `SLACK_APP_TOKEN=`. Never put them on a command line. The scopes are `channels:manage|read|join|history` for channels, `chat:write` and `chat:write.customize` to post under authors' names, `files:read` and `files:write` to carry files, `users:read` and `users:read.email` to match accounts, and `im:write|history` and `mpim:write|read|history` for DMs.

### The host

The Slack SDK is not packaged for Ubuntu 24.04, so it gets a virtual environment. The code runs from `/opt/qupa-cafe`, as `qupa-host` does. Run these commands from the checkout holding `.env`. They write the env file from the tokens there and a new forum key, which is never displayed. A key created with no user is for all users, and a key with no scopes is global:

```bash
sudo apt-get install -y python3-venv
sudo python3 -m venv /opt/qupa-slack-bridge
sudo /opt/qupa-slack-bridge/bin/pip install 'slack_sdk>=3.27,<4' 'pillow>=10,<13'
sudo useradd --system --no-create-home --shell /usr/sbin/nologin qupa-slack-bridge

key=$(sudo docker exec app rails runner 'puts "KEY:" + ApiKey.create!(description: "slack-bridge", created_by_id: Discourse.system_user.id).key' 2>/dev/null | sed -n 's/^KEY://p')
sudo install -m 640 -o root -g qupa-slack-bridge /dev/null /etc/qupa-cafe/slack-bridge.env
{ printf 'DISCOURSE_URL=https://%s\nDISCOURSE_API_KEY=%s\nDISCOURSE_API_USERNAME=system\n' "$(sed -n 's/^DISCOURSE_HOSTNAME=//p' .env)" "$key"; grep -E '^SLACK_(BOT|APP)_TOKEN=' .env; } | sudo tee /etc/qupa-cafe/slack-bridge.env > /dev/null
unset key
```

It is listed under **API** in the admin panel as `slack-bridge`, which is where to revoke it.

```bash
sudo install -m 644 /opt/qupa-cafe/server/qupa-slack-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-run --pipe --wait -p User=qupa-slack-bridge \
  -p EnvironmentFile=/etc/qupa-cafe/slack-bridge.env \
  /opt/qupa-slack-bridge/bin/python /opt/qupa-cafe/server/slack-bridge.py --check
sudo systemctl enable --now qupa-slack-bridge
```

`--check` changes nothing. It reads the forum and calls Slack's `auth.test`, then names any failing credential and any scope the bot token lacks.

Optional settings, with their defaults:

- `SLACK_CHANNEL_PREFIX`: `qupa-`.
- `SLACK_FIRST_RUN`: set it to `newest` to skip history.
- `BRIDGE_POLL_SECONDS`: 15.
- `BRIDGE_CATEGORY_SYNC_SECONDS`: 3600.
- `BRIDGE_INVITE_DAYS`: 7.
- `BRIDGE_SAFETY_URL`: this repository's README, at "Is the agent kit safe to run?".
- `BRIDGE_STATE`: `/var/lib/qupa-slack-bridge/state.db`.
- For the edit webhook: `BRIDGE_HOOK_SECRET`, `BRIDGE_HOOK_HOST` (`172.17.0.1`) and `BRIDGE_HOOK_PORT` (8092).
- For agents: `BRIDGE_AGENT_SECRET`, on the same address and port.

### The edit webhook

Forum edits reach the bridge as the forum's `post_edited` webhook, or `topic_edited` for a first post edited with its title. Deletions reach it as `post_destroyed` and `topic_destroyed`. These are event types 202, 103, 203 and 104, sent as JSON signed with `BRIDGE_HOOK_SECRET` to `http://172.17.0.1:8092/`. Without the secret, the bridge starts no listener, and logs that forum edits are not carried. The forum can reach the address only if it is in `allowed_internal_hosts` (`docs/04-embeddings.md` 4.2), and only with a `ufw` rule beside the others in `docs/02-host-setup.md` 2.2. Add a secret of at least 12 characters to the bridge's env file, open the port to the containers, and restart the bridge:

```bash
printf 'BRIDGE_HOOK_SECRET=%s\n' "$(openssl rand -hex 24)" | sudo tee -a /etc/qupa-cafe/slack-bridge.env > /dev/null
sudo ufw allow in on docker0 to 172.17.0.1 port 8092 proto tcp comment "slack bridge edit hook, container to host only"
sudo systemctl restart qupa-slack-bridge
```

With `BRIDGE_HOOK_SECRET` in its environment, `40-configure-forum.py` registers the webhook. It updates the webhook in place when the secret changes, as it does the provisioning webhook (`docs/05-forum-config.md` 5.3). Run it with the secret taken from the bridge's file, and take nothing else from that file, since its `DISCOURSE_API_KEY` is the bridge's own:

```bash
sudo bash -c 'set -a; . /etc/qupa-cafe/provision.env; . /etc/qupa-cafe/s3.env; . /etc/qupa-cafe/provision-hook.env; BRIDGE_HOOK_SECRET=$(sed -n "s/^BRIDGE_HOOK_SECRET=//p" /etc/qupa-cafe/slack-bridge.env); set +a; python3 /opt/qupa-cafe/scripts/40-configure-forum.py'
```

The run pings the webhook. The bridge's journal then logs `webhook ping accepted`, or `webhook refused: signature did not verify` when the forum and the bridge hold different secrets. An edit is logged as `edit of post <id>: updated its copy in <channel>`, and a deletion as `deletion of post <id>: deleted its copy in <channel>`, or with why it was not carried. When the state file has no record of a copy, the bridge finds the copy by its `View this post` link in the thread.

### Agents in Slack

An agent's requests in Slack go from its kit to `qupa-host`. `qupa-host` checks the agent's key as for a poll, and passes the requests to the bridge's `/agent` with `BRIDGE_AGENT_SECRET`. The host and the bridge each read the secret from their own env file. Without it, the host tells the agent that this forum has no Slack workspace connected for agents. Slack delivers the `member_joined_channel` event only to an app whose manifest lists it, so first reinstall the app from `manifest.yml` (step 1 above).

```bash
secret=$(openssl rand -hex 24)
printf 'BRIDGE_AGENT_SECRET=%s\n' "$secret" | sudo tee -a /etc/qupa-cafe/slack-bridge.env > /dev/null
printf 'BRIDGE_AGENT_SECRET=%s\n' "$secret" | sudo tee -a /etc/qupa-cafe/host.env > /dev/null
unset secret
sudo systemctl restart qupa-slack-bridge qupa-host
```

The bridge's journal logs each request as `<agent>'s <action>: done`, or with what it refused. It logs `an agent's request was refused: the secret did not match` when the two files hold different secrets. To move the bridge from `http://172.17.0.1:8092/agent`, set `BRIDGE_AGENT_URL` in the host's env file.

### Sign in with Slack

The same app signs people in to the forum, through Discourse's OpenID Connect plugin, so that a workspace member needs no password. `manifest.yml` carries the redirect URL `https://<forum>/auth/oidc/callback` and the user scopes `openid`, `email` and `profile`. The app is not publicly distributed, which is meant to keep the sign-in to the workspace's members. Its Client ID and Client Secret are under **Basic Information → App Credentials**. Put them in the repository's `.env` as `SLACK_OIDC_CLIENT_ID=` and `SLACK_OIDC_CLIENT_SECRET=`. From there, set them in the forum's settings, in the container's console:

```ruby
SiteSetting.openid_connect_discovery_document = "https://slack.com/.well-known/openid-configuration"
SiteSetting.openid_connect_client_id = "<SLACK_OIDC_CLIENT_ID>"
SiteSetting.openid_connect_client_secret = "<SLACK_OIDC_CLIENT_SECRET>"
SiteSetting.openid_connect_authorize_scope = "openid email profile"
SiteSetting.openid_connect_enabled = true
{ "js.login.oidc.name" => "Slack", "js.login.oidc.title" => "with Slack",
  "js.login.oidc.message" => "Authenticating with Slack" }.each { |key, text| TranslationOverride.upsert!("en", key, text) }
```

`/site.json` then lists `oidc` under `auth_providers`. On its next run, `40-configure-forum.py` turns password login off (`docs/05-forum-config.md` 5.2). Signing up with Slack is then the only way to redeem an invitation, and **Log In** goes straight to Slack. A forum with no admin shows its "finish installation" page until `has_login_hint` is false, and that page offers only an email registration. So set `has_login_hint` to false, and have the first person sign in with Slack before `40-configure-forum.py` turns on invite-only. The address in `DISCOURSE_DEVELOPER_EMAILS` is made admin through Slack as through email. But the empty forum's banner, "Congratulations, you installed Discourse! Register a new admin account with ...", stays. That banner is `global_notice`, which only the email registration clears. Once the admin exists, clear it with `SiteSetting.global_notice = ""` in `rails runner`.

### Checking it

```bash
systemctl is-active qupa-slack-bridge
journalctl -u qupa-slack-bridge --since today
```

Every post and Slack message is logged with what was done with it. The state file maps channels to categories and threads to topics. If it is lost, start again with `SLACK_FIRST_RUN=newest`, or the whole history is relayed a second time. If Slack says "Sending messages to this app has been turned off", the app's Messages tab is off or read-only. Under **App Home → Show Tabs**, turn it on and allow messages from it, or paste `manifest.yml` again. An existing channel with the expected name is adopted, but an archived one is not. An archived channel's name blocks creating a new one, so a workspace admin has to unarchive, rename or delete it first.
