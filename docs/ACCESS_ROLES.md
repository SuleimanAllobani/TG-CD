# Roles, teams and limits (per agent)

Every agent has its own list of people. Roles are checked **in the bot** before anything is sent to the backend or the agent
(and again when the final "confirm" is pressed, so a role change takes effect immediately, even in the middle of a conversation).

| Role | Can do |
|---|---|
| viewer | Status, Releases, History, Logs, Analyze, Report, `/agent_users`, `/my_access` |
| deployer | everything a viewer can, plus Deploy (ZIP, folder, GitHub) and `/rollback` |
| owner | everything, plus `/delete_release`, deleting the agent, and managing people, teams and limits |

* The person who creates an agent (Setup Agent) is its owner. An agent can have several owners; the last owner cannot be removed or demoted.
* Old agent records keep working: the old role name `member` (or any unknown name) behaves as `deployer`, and people without limits are unlimited, exactly as before.
* A person's role is the highest of their own role and the roles of their teams.

## Commands (owners only, for the currently selected agent)

| Command | Meaning |
|---|---|
| `/add_user <telegram_id> [viewer\|deployer\|owner]` | Give a person access (default deployer). They get a Telegram notice if they have started the bot. They find their ID with `/myid`. |
| `/set_role <telegram_id> <role>` | Change a role. |
| `/remove_user <telegram_id>` | Remove access, team memberships and limits. |
| `/team_create <name> <viewer\|deployer>` | New team. It starts with **no** GitHub repositories and **no** credentials. |
| `/team_add <team> <telegram_id>` / `/team_remove <team> <telegram_id>` | Manage members. Someone added to a team who has no access yet becomes a viewer with no GitHub rights of their own; the team decides the rest. |
| `/team_role <team> <viewer\|deployer>` / `/team_delete <team>` | Change or delete a team. |
| `/allow_repos <telegram_id\|team:NAME> <owner/repo,owner/*\|all\|none>` | Which GitHub repositories the person or team may deploy. |
| `/allow_creds <telegram_id\|team:NAME> <name1,name2\|all\|none>` | Which private-repository credential names they may use. |
| `/agent_users` | Everyone's role, teams and limits (any role may view). |
| `/my_access` | Your own role and limits. |

## Limits

* `repos` and `creds` apply to GitHub deployments only. ZIP and folder deployments need only the deployer role.
* A limit is the union of everything that applies to the person: their own entry (if any) and each team. If one of them is `all`, the person is unlimited. Owners are never limited.
* Public repositories use no credential, so the credential limit does not apply to them.
* The agent's own `git.allowed_repos` (agent settings) stays the last line of defence on the IIS machine and applies to everybody, including owners.

## Other protections added with roles

* Delete agent is owner-only, and nothing (not even the backend record) is removed before that check.
* `/register_agent` and Setup Agent can no longer overwrite an agent ID that already exists and belongs to somebody else.
* Logs, Report, Analyze and download-logs only show jobs of the currently selected agent; `/history <agent_id>` needs access to that agent.

## What roles do not do

* Roles are per agent, not per IIS site: a deployer can deploy to any site name on that agent. Use separate agents for strictly separate environments.
* The tokens themselves are still stored on the IIS machine. A credential *name* limit controls who may use a name, not who can read the file on the server.
