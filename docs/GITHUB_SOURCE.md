# GitHub as a deployment source

GitHub is an **additional source provider** next to *Upload ZIP* and *Folder path*. It only produces the
files of a release. Versioning, IIS configuration, health check, automatic rollback, job history and the
analyzer are the existing code, shared by all three sources.

```
GitHub repo ──► GitSourceService.stage()  ──► temp folder (no .git)
                                                  │
                  ZIP ──► extract_zip ────────────┤
               Folder ──► copy_folder ────────────┤
                                                  ▼
                  release folder ─► IIS ensure ─► switch current ─► recycle ─► health check ─► (rollback)
```

The repository is fetched **before** a release is created. A wrong URL/ref/token therefore fails with a clear
message and touches nothing (no release, no IIS change, no rollback).

## Using it in Telegram

Deploy → agent → project type **Static** or **.NET** → **GitHub repository**, then:

1. Repository URL (`https://github.com/owner/repo`; `github.com/owner/repo` also works).
2. Public or private. Private asks for a **credential name** (see below), never a token.
3. Branch, tag or commit (full or short hash) - or *Default branch*.
4. Whole repository or one sub-folder (for example `docs` or `public`) - or *Repository root*.
5. Build. Static: *No build* (files as they are) or *npm build* (then the output folder, or auto-detect).
   .NET: the `.csproj` to publish, or auto-detect. See "Builds" below.
6. The usual questions (site name, port, app pool, health URL) and confirmation.

`.git` and `.github` are not deployed. Submodules and Git LFS files are not fetched (a warning is recorded).

History, Releases, Report and the failure analyzer show `owner/repo@ref (commit)`.

## Builds (.NET publish and npm build)

The build is an additional step between *fetch* and *release*:
`fetch → build → release folder → IIS → health check → rollback`. The build runs **before** a release exists, so a
failing build changes nothing on IIS and needs no rollback; the previous release keeps running.

| Preset | Project type | What runs | Deployed |
|---|---|---|---|
| `npm_build` | Static | `npm ci` (with `package-lock.json`/`npm-shrinkwrap.json`) or `npm install`, then `npm run build` | the output folder (`dist`, `build`, `out`, `_site`, or the one you name) |
| `dotnet_publish` | .NET | `dotnet publish <project.csproj> -c Release -o <folder>` | the publish output (includes `web.config`) |

Safety rules: only these two fixed presets exist (no free-text commands from Telegram); builds run **only** for
repositories listed in `git.allowed_repos` (an empty list means no builds; plain file deployments are unaffected); the
build gets a minimal environment (no agent, bot, backend or GitHub tokens, no `GIT_*` variables); a hard timeout
(`build_timeout_seconds`, default 900) kills the whole process tree; output is redacted and shortened.

Requirements on the agent machine: Node.js LTS for npm builds; the .NET SDK (not only the runtime) and the ASP.NET
Core Hosting Bundle for .NET. Not supported: yarn/pnpm-only projects (add `package-lock.json` or use *No build* with a
pre-built folder), .NET Framework (non-Core) projects, projects needing private package feeds that need credentials.
.NET auto-detect ignores test projects and needs exactly one web project, otherwise it lists the candidates.

Extra settings: `"build_timeout_seconds": 900`, `"dotnet": "C:\\Program Files\\dotnet\\dotnet.exe"`,
`"npm": "C:\\Program Files\\nodejs\\npm.cmd"`, `"build_env_passthrough": ["MY_VARIABLE"]` (extra variable names the build may see).

## What is recorded

For every GitHub deployment (success, failure, rolled back) the result and the agent's `releases.json` contain:

```json
"source": {
  "type": "github", "repo_url": "https://github.com/owner/repo", "repo": "owner/repo",
  "requested_ref": "main", "ref_type": "branch", "resolved_ref": "refs/heads/main",
  "commit": "<40-char sha>", "commit_short": "abc1234", "commit_subject": "...", "commit_date": "...",
  "subdir": null, "auth": "anonymous" | "token:<credential name>",
  "build": {"preset": "npm_build", "target": null, "tool": "npm 10.x", "seconds": 12.3, "output": "dist"}   // only when a build ran
}
```

ZIP and folder deployments are unchanged: no `source` key is added to their results or release entries.
Release metadata is deliberately **not** written into the site folder, because that folder is served by IIS.

## Private repositories

The token lives only on the agent (IIS) machine. Telegram and the backend carry only its *name*.

1. On GitHub create a **fine-grained personal access token**: only the needed repositories,
   permission *Contents: Read-only*, with an expiry date.
2. On the agent machine, as Administrator (in the agent folder):

   ```cmd
   scripts\set_git_token.bat --alias default --verify owner/private-repo
   ```

   The token is typed at a hidden prompt and saved to `secrets\github_default.token` (SYSTEM and
   Administrators only). Alternatives: environment variable `CICD_GIT_TOKEN_DEFAULT`, or
   `"git": {"credentials": {"default": {"token_env": "MY_VAR"}}}` in `config\settings.json`.
3. In Telegram choose *GitHub repository → Private* and send `default`.

How the token is protected: it is passed to git through the child process **environment** as an
`Authorization` header scoped to `https://github.com/` (never on the command line, in the URL, in
`.git/config`, in a job payload, a log or a result); every message is redacted; the backend refuses job payloads
that contain anything token-like; the bot deletes and ignores messages that look like tokens and tells the user to
revoke them; inherited `GIT_TRACE`/`GIT_CURL_VERBOSE`/`GIT_ASKPASS`-style variables are not passed to git.

## Agent settings (`config\settings.json`, all optional)

```json
"git": {
  "allowed_repos": ["owner/*", "owner/special-repo"],
  "timeout_seconds": 300,
  "max_repo_mb": 500,
  "build_timeout_seconds": 900,
  "executable": "C:\\Program Files\\Git\\cmd\\git.exe",
  "secrets_dir": "C:\\CICD_AGENT\\secrets",
  "credentials": {"default": {"token_env": "MY_VAR", "token_file": "other.token"}}
}
```

An empty/missing `allowed_repos` allows any github.com repository for plain file deployments, but **never** allows builds; set it on production agents.

## Rolling it out

* The agent package contains copies of `agent/` and `common/`. **Regenerate the agent ZIP** (Setup Agent) and
  reinstall, or copy the changed files, on every agent that should support GitHub. An old agent fails safely with
  `Invalid source_type`.
* Git for Windows must be installed on the agent (the installer tries `winget install Git.Git`).
* Restart the bot and backend services after updating the code. Existing jobs and the database are compatible.

## Tests

`python -m unittest discover -s tests -t .` (needs `git` on PATH). They use local git repositories that stand in
for github.com, a local HTTP git server with Basic authentication for the private-repository path, the mock IIS, and
a fake Telegram/back-end for the bot conversation.
