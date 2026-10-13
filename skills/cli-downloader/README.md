# CLI-Downloader Agent Skill

The CLI-Downloader Agent Skill teaches a compatible AI coding agent how to operate CLI-Downloader safely and how to research, create, validate, and document external website plugins.

It follows the [open Agent Skills specification](https://agentskills.io/specification): `SKILL.md` is the entry point, while `references/` and `assets/` provide detailed workflows and a reusable plugin template.

## Capabilities

- Inspect persisted tasks and download history through the public CLI.
- Validate URL routing with `--dry-run` before a live download.
- Add and verify downloads after explicit user authorization.
- Submit authorized work to the single visible Shell owner and control the accepted Task.
- Treat a command-line list URL without `--select` as authorization-sensitive bulk scope.
- Diagnose unsupported URLs and plugin routing failures.
- Research a representative website URL before implementing support.
- Scaffold and document an external CLI-Downloader plugin.

The Skill does not install CLI-Downloader itself, install website plugins, bypass agent permissions, or authorize network and filesystem actions on the user's behalf.

## Requirements

- A local CLI-Downloader installation providing `clid`, or a CLI-Downloader source checkout that can run `python -m cli_downloader`.
- An Agent Skills-compatible environment.
- The complete `skills/cli-downloader` directory. Do not copy only `SKILL.md`; the Skill uses its bundled references and template assets.

You can perform the installation yourself, or ask a compatible Agent to install the Skill from this checkout. For example: `Install the CLI-Downloader Agent Skill from this repository and verify that it is available.` Review the Agent's proposed file operations and approve them according to your environment's permission rules.

All commands below assume the current directory is the root of a cloned CLI-Downloader repository.

## Codex

Codex discovers personal skills from `$HOME/.agents/skills` and repository skills from `.agents/skills`.

Official reference: [Build skills in Codex](https://learn.chatgpt.com/docs/build-skills).

### Personal Installation

Available in every project for the current user:

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
Copy-Item -Recurse -Force ".\skills\cli-downloader" "$HOME\.agents\skills\"
```

Expected entry point:

```text
$HOME/.agents/skills/cli-downloader/SKILL.md
```

### Project Installation

Available only while Codex works in the target repository:

```powershell
New-Item -ItemType Directory -Force "<project>\.agents\skills" | Out-Null
Copy-Item -Recurse -Force ".\skills\cli-downloader" "<project>\.agents\skills\"
```

Start a new Codex session if the Skill does not appear after installation. Invoke it explicitly with `$cli-downloader`, or describe a matching task and allow Codex to select it automatically.

## Claude Code

Claude Code discovers personal skills from `$HOME/.claude/skills` and project skills from `.claude/skills`.

Official reference: [Extend Claude Code with skills](https://code.claude.com/docs/en/skills).

### Personal Installation

Available in every project for the current user:

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null
Copy-Item -Recurse -Force ".\skills\cli-downloader" "$HOME\.claude\skills\"
```

Expected entry point:

```text
$HOME/.claude/skills/cli-downloader/SKILL.md
```

### Project Installation

Available only while Claude Code works in the target repository:

```powershell
New-Item -ItemType Directory -Force "<project>\.claude\skills" | Out-Null
Copy-Item -Recurse -Force ".\skills\cli-downloader" "<project>\.claude\skills\"
```

Claude Code normally detects changes in an existing skills directory. Restart it if the top-level skills directory was created after the current session started. Invoke the Skill explicitly with `/cli-downloader`, or describe a matching task and allow Claude to select it automatically.

## Other Agent Skills Environments

This package uses the open Agent Skills structure. For another compatible agent:

1. Find the product's documented personal or project skill directory.
2. Copy the complete `skills/cli-downloader` directory into it.
3. Confirm that the resulting path ends with `cli-downloader/SKILL.md`.
4. Restart the agent if newly installed skills are not detected.
5. Ask the agent to use `cli-downloader`, then verify that it reads `SKILL.md` before acting.

Do not assume that every agent supports automatic discovery, explicit invocation, the same permission model, or the optional `agents/openai.yaml` metadata.

## Verify the Installation

Try a read-only request first:

```text
Use the cli-downloader skill to show the current tasks without creating new state.
```

The agent should use `clid tasks list --json` when CLI-Downloader is installed, keep Tasks distinct from History, and avoid starting a network request or download.

For a live Batch or list URL, the agent should dry-run first and obtain authorization for all
items returned by the operation, or pass repeated `--select <item-id>` values for a requested
subset. The visible Shell keeps its interactive selection page.

## Update

Pull the latest CLI-Downloader repository changes, then repeat the same `Copy-Item` command used during installation. `-Force` updates existing files while retaining the expected directory name.

Review changes to `SKILL.md`, `references/`, and `assets/` before updating. A Skill is executable guidance for an agent and should be treated as trusted local configuration.

## Usage Examples

**Checking current downloads**
```text
Use the cli-downloader skill to check what's currently downloading and show my recent history.
```

**Testing a URL before committing to a download**
```text
Use the cli-downloader skill to dry-run this link and tell me which plugin would handle it:
https://example.com/watch?v=abc123
```

**Building a plugin for a specific site**
```text
Use the cli-downloader skill to build a plugin for xxx.com. Downloads should follow this URL pattern:
https://xxx.com/video/12345 — research the page structure first and show me the plan before writing any code.
```

```text
Use the cli-downloader skill to add support for downloading playlists from music.example.com. Ask me before running any live network requests.
```

**Debugging a plugin that isn't routing correctly**
```text
Use the cli-downloader skill to figure out why this URL isn't matching any plugin, and suggest a fix:
https://example.com/embed/xyz
```

**Documenting a finished plugin**
```text
Use the cli-downloader skill to write the README and usage docs for the plugin I just built for xxx.com.
```

For the complete workflow and safety rules, see [SKILL.md](SKILL.md).
