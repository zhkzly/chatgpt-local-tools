# Personal KDocs and WPS365

Verified against official documentation on 2026-11-10; recheck installed help
before executing because CLI command schemas can change.

## Choose the correct product

| Product | Interface and account |
| --- | --- |
| Personal KDocs, `kdocs-cli` | Official `kdocs-app/kdocs-skill`; personal WPS accounts and kdocs.cn cloud documents. |
| Enterprise WPS365, `wps365-cli` | Separate enterprise application, scopes/approval; already has `mcp serve`. |
| WPS Office `wpscli` | Document/PDF processing; do not assume access to a personal cloud drive. |

The personal official auth guide explicitly limits `kdocs-cli` to personal accounts.
Do not route personal users into enterprise onboarding merely because both say WPS.

## Install and authenticate

Follow the current [official repository](https://github.com/kdocs-app/kdocs-skill).
Its setup supports Linux amd64/arm64 and normally installs under `~/.local/bin`.
Review the installer before running it; use the complete official checkout so its
version metadata is available. This project neither bundles nor automatically
executes that installer. Upstream checksum handling is not a guarantee of a forced
check on every environment, so do not claim otherwise.

```bash
kdocs-cli version
kdocs-cli help
kdocs-cli auth login
kdocs-cli auth status
kdocs-cli help drive.search_files
kdocs-cli help drive.read_file
```

Complete login locally using the same OS user/session as the runtime, so the
official CLI can use its system keychain. Do not copy tokens/cookies into our
config or publish auth commands as tools. If a sandbox or background session
cannot access that keychain, report the actual problem and use an authorized
host/session; never bypass it by extracting credentials from the browser.

## Read-only example

`assets/kdocs-readonly.adapter.json` exposes fixed commands for search, personal
root listing, star listing and document reading. `assets/kdocs.connection.json`
runs that adapter through the main stdio MCP channel. It does not perform writes,
login or token export. The example was checked against official help/documents,
but no real user KDocs account was used in this project's tests.

```bash
kdocs-cli drive search-files --keyword "example" --type all --page-size 10
kdocs-cli drive list-my-files --page-size 10
kdocs-cli drive list-star-items --page-size 10
kdocs-cli drive read-file --url "https://www.kdocs.cn/l/YOUR_LINK"
```

These are fixed-size initial pages. Do not call them exhaustive results. `read_file`
can return `pending` and a `task_id`; use the original file locator plus that exact
returned task ID to continue, using the bundled polling tool after checking local
help. Honor partial-read warnings. Stop on whole-file permission denial and obey
the documented retry time for rate limits; do not change interfaces to evade them.

The adapter's success describes the transport/process. Read CLI business codes
before saying a document was read. A download-info operation is not necessarily a
saved local file; a local download is not automatically an attachment in ChatGPT.
This connection does not sync a cloud drive to Google Drive.

## Existing MCP options

For WPS365, verify installed `wps365-cli mcp --help` and use its documented native
`wps365-cli mcp serve` after enterprise authorization. Do not wrap it again.
There is evidence of older personal KDocs MCP endpoints, but this project has not
verified their current ChatGPT authentication/discovery; do not advertise a stale
URL as a working one-click connection. Prefer a current suitable public MCP when
officially supported and verified.

## Primary sources

- [Personal Skill](https://github.com/kdocs-app/kdocs-skill/blob/master/SKILL.md)
- [Personal account authentication](https://github.com/kdocs-app/kdocs-skill/blob/master/references/auth.md)
- [Drive operations](https://github.com/kdocs-app/kdocs-skill/blob/master/skills/kdocs-drive/SKILL.md)
- [File locating](https://github.com/kdocs-app/kdocs-skill/blob/master/skills/kdocs-drive/references/file-locating-guide.md)
- [Official installer](https://github.com/kdocs-app/kdocs-skill/blob/master/scripts/setup.sh)
- [WPS365 MCP changelog](https://github.com/wps365-open/cli/blob/main/CHANGELOG.md)

Only original integration instructions/configuration are included here; vendor
Skills/installers are linked, not copied or relicensed under this project's MIT license.
