# Agent Customization Guide

## Overview

OpenClose uses `[[agents]]` sections in `config.toml` to define and customize agents. This controls:

- Which model each agent uses
- What system prompt the agent receives
- Which tools the agent can see and call (custom agents only)
- Semantic traits that drive runtime behaviour

## Built-in Agents

Two switchable agents are always available with **locked tool restrictions**:

| Agent | Mode | Description | Tool restrictions |
|-------|------|-------------|-------------------|
| `build` | primary | Full tool access for coding | Denied: plan |
| `plan` | primary | Read-only analysis (bash allowed for verification only) | Denied: write, edit, multiedit, browser_automation, deliver_message |

**Locked fields:** The `traits`, `allowed_tools`, and `denied_tools` of built-in agents cannot be overridden by user config. This ensures consistent behaviour regardless of configuration.

**Customizable fields:** You can override `model`, `temperature`, `max_steps`, `description`, and `system_prompt` for built-in agents.

> **Note on `delegate`:** `delegate` is a *tool*, not an agent — it spawns a read-only sub-agent internally and is not switchable from `/agents`. Its sampling temperature lives in `[temperatures] delegate = X` (see the next section). `[[agents]] name = "delegate"` blocks are reserved and ignored with a warning.
>
> **Note on the `plan` reviewer:** the `plan` tool itself is callable by the `plan` agent, but in its `phase="draft"` mode it spawns its own read-only reviewer sub-agent (analogous to `delegate`'s sub-agent). That reviewer's sampling temperature is `[temperatures] plan_reviewer = X` (see below). The `plan` *agent* is still configurable via `[[agents]] name = "plan"`; only the reviewer sub-agent's temperature lives in `[temperatures]`.

## Non-agent LLM temperatures

LLM calls that aren't primary agents — tool-internal one-shot calls (recorder annotators, browser automation planners, the cron-NL parser, the skill builder/runner), the read-only sub-agent spawned by the `delegate` tool, and the read-only reviewer sub-agent spawned by the `plan` tool when called with `phase="draft"` — are configured here, separately from `[[agents]]`, so they remain centrally tunable without overlapping the agent registry.

```toml
[temperatures]
skills_runner             = 0.1
skills_builder            = 0.1
browser_vision_grounding  = 0.0
browser_vision_planner    = 0.0
browser_dom_planner       = 0.0
recorder_merger           = 0.1
recorder_task_builder     = 0.1
recorder_chunk_annotator  = 0.2
cron_nl                   = 0.0
delegate                  = 0.0
plan_reviewer             = 0.0
```

| Field | Default | Used by |
|-------|---------|---------|
| `skills_runner` | 0.1 | Headless skill execution (`skills/runner.py`) |
| `skills_builder` | 0.1 | Distilling a session into a skill form (`skills/builder.py`) |
| `browser_vision_grounding` | 0.0 | Pixel grounding in vision-mode browser automation — keep low for deterministic clicks |
| `browser_vision_planner` | 0.0 | Vision-mode action planner |
| `browser_dom_planner` | 0.0 | DOM-mode browser automation planner |
| `recorder_merger` | 0.1 | Merging per-chunk procedures into a single recording (`recorder/merger.py`) |
| `recorder_task_builder` | 0.1 | Annotating a procedure with task structure (`recorder/task_builder.py`) |
| `recorder_chunk_annotator` | 0.2 | Multimodal chunk annotation (`recorder/chunk_annotator.py`) |
| `cron_nl` | 0.0 | Natural-language → cron expression translation (`jobs/cron_nl.py`) |
| `delegate` | 0.0 | Read-only sub-agent spawned by the `delegate` tool (`tool/tools/delegate.py`) |
| `plan_reviewer` | 0.0 | Read-only reviewer sub-agent spawned by the `plan` tool when called with `phase="draft"` (`tool/tools/plan.py`) |

Each field is independently overridable; omitted keys keep their default. Validation is the same as agent temperatures: each must be in `[0.0, 2.0]`.

## Configuration Format

Agents are defined as `[[agents]]` sections in `config.toml`:

```toml
[[agents]]
name = "build"
description = "Primary coding agent"
model = "your-model-name"
temperature = 0.0
max_steps = 100
```

### All Available Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | *(required)* | Unique agent name |
| `description` | string | `""` | Short description of the agent's purpose |
| `model` | string | `""` | Model ID to use. Empty = auto-detect from provider |
| `temperature` | float | `0.0` | Sampling temperature (0.0–2.0) |
| `max_steps` | int | `100` | Maximum tool-call loop iterations (must be > 0) |
| `mode` | string | `"primary"` | Currently only `"primary"` is supported |
| `system_prompt` | string | `""` | Inline system prompt |
| `traits` | list of strings | `[]` | Semantic behaviour flags (e.g. `["readonly"]`) |
| `allowed_tools` | list of strings | `[]` | Allowlist: if non-empty, only these tools are available |
| `denied_tools` | list of strings | `[]` | Denylist: these tools are hidden from the agent |

## Quick Start Examples

### 1. Override the default build agent's model

```toml
# <config_dir>/config.toml (or .openclose/config.toml in the project)

[[agents]]
name = "build"
model = "your-model-name"
```

That's it. Everything else (description, tools, prompt) keeps its default.

### 2. Create a read-only reviewer agent

```toml
[[agents]]
name = "reviewer"
description = "Reviews code for correctness and security"
model = "your-model-name"
temperature = 0.3
traits = ["readonly"]
allowed_tools = ["read", "grep", "glob"]
system_prompt = """You are a senior code reviewer. Focus on:
- Correctness and edge cases
- Security vulnerabilities (OWASP Top 10)
- Code clarity and maintainability
Do NOT suggest stylistic changes unless they affect readability."""
```

### 3. Create a research agent

```toml
[[agents]]
name = "researcher"
description = "Deep research agent for codebase analysis"
model = "your-model-name"
temperature = 0.0
max_steps = 50
allowed_tools = ["read", "grep", "glob", "webfetch", "delegate"]
denied_tools = ["write", "edit", "bash"]
system_prompt = """You are a research agent specializing in codebase analysis.
You are working in $project_dir.

Your job:
- Explore the codebase thoroughly before answering
- Cite specific file paths and line numbers
- Provide structured summaries

You have access to: $tool_names"""
```

## Prompt System

### Two-Layer Architecture

All agents receive a **common prompt** (Layer 1) that establishes the AI coding assistant role. Then each agent gets an **agent-specific prompt** (Layer 2):

- **build**: Instructions for full code writing and testing
- **plan**: Read-only mode with planning focus
- **Custom agents**: Your `system_prompt` field
- **Sub-agents** (delegate): Built-in specialised prompts

If you set `system_prompt` on a built-in agent, it replaces the default Layer 2 while the common prompt (Layer 1) is always present.

### Template Variables

Prompts support `$variable` or `${variable}` placeholders:

| Variable | Value |
|----------|-------|
| `$project_dir` | Working directory |
| `$date` | Current date (YYYY-MM-DD) |
| `$agent_name` | Agent name |
| `$agent_description` | Agent description |
| `$tool_names` | Comma-separated list of available tools |

Unknown variables are left as-is (safe substitution).

**Example:**

```toml
[[agents]]
name = "myagent"
system_prompt = """You are $agent_name, working in $project_dir.
Today is $date.

You have access to: $tool_names

$agent_description"""
```

## Traits

Traits are semantic flags stored on agents. Built-in agents have locked traits:

| Agent | Traits |
|-------|--------|
| `build` | *(none)* |
| `plan` | `readonly`, `plan` |

Custom agents can define any traits they wish. Traits are accessible via `agent.has_trait("name")`.

## Tool Visibility

Tools are filtered **before they reach the LLM** — the model never sees tools it cannot use.

### `allowed_tools` (allowlist)

If non-empty, **only** these tools are visible:

```toml
[[agents]]
name = "researcher"
allowed_tools = ["read", "grep", "glob", "webfetch"]
# The agent cannot see or call write, edit, bash, etc.
```

### `denied_tools` (denylist)

These tools are hidden:

```toml
[[agents]]
name = "safe-coder"
denied_tools = ["bash"]
# The agent can see and call everything except bash
```

### No restrictions

If both lists are empty, the agent sees all registered tools.

### Available Built-in Tools

| Tool | Description |
|------|-------------|
| `read` | Read file contents |
| `write` | Create or overwrite files |
| `edit` | Find-and-replace code edits |
| `multiedit` | Multiple edits in one file |
| `glob` | File pattern matching |
| `grep` | Content search (ripgrep) |
| `bash` | Shell command execution |
| `webfetch` | HTTP fetch + plaintext conversion |
| `plan` | Two-phase plan tool (`plan` agent only). `phase="draft"` spawns a read-only reviewer sub-agent that critiques the plan against actual code and returns concrete edits; `phase="final"` presents the polished plan to the user for review. |
| `ask_user` | Prompt the user for input during a run |
| `delegate` | Read-only sub-agent for focused investigations with configurable budget (up to 5 calls in parallel) |
| `browser_automation_dom` | Fast accessibility-tree browser navigation (preferred) |
| `browser_automation_vision` | Screenshot + visual-grounding browser navigation (escalation / Vision Mode) |
| `deliver_message` | Send text to Telegram/Discord via configured channel aliases |

## Complete Example

```toml
# config.toml

# ── Customize built-in build agent ──────────────────────────
[[agents]]
name = "build"
description = "Full-stack dev agent"
model = "your-model-name"
temperature = 0.0
max_steps = 150
system_prompt = """You are a coding agent working in $project_dir.
Write clean, tested code. Follow the project's conventions."""

# ── Customize built-in plan agent ───────────────────────────
[[agents]]
name = "plan"
max_steps = 120

# ── Custom reviewer (fully self-contained) ──────────────────
[[agents]]
name = "reviewer"
description = "Code review agent"
model = "your-model-name"
temperature = 0.3
traits = ["readonly"]
allowed_tools = ["read", "grep", "glob"]
system_prompt = """You are a code reviewer. Focus on correctness, security, and clarity."""

# ── Quick-fix agent (fully self-contained) ──────────────────
[[agents]]
name = "hotfix"
description = "Minimal targeted fixes only"
model = "your-model-name"
max_steps = 20
denied_tools = ["plan"]
system_prompt = """You are a hotfix agent. Make the SMALLEST possible change
to fix the reported issue. Do not refactor, do not add tests, do not improve
surrounding code. One surgical edit only."""
```

## Troubleshooting

**Agent not found**
Check that the name matches exactly (case-sensitive). Run `GET /api/agents` to see all loaded agents.

**Locked field warning in logs**
If you see `Field 'denied_tools' is locked for built-in agent 'build' -- ignored`, it means you tried to override a locked field on a built-in agent. Remove that field from your config.

**Tool still visible after adding to denied_tools**
Make sure you're editing the correct `config.toml` (project-level overrides user-level). Restart the server after changes.
