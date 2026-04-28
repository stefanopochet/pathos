# pathos

External supervisor for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents.

Pathos watches your Claude Code session in real time, catches quality issues before they compound, and injects corrections directly into the agent's conversation — no manual review required.

## How it works

```
pathos → tmux session → claude (interactive)
           ↓
     supervisor (background)
           ↓
     polls JSONL conversation log
           ↓
     Haiku triage (fast, flag generously)
           ↓
     Sonnet validation (forensic, high bar)
           ↓
     tmux injection + sound alert
```

Two-stage pipeline: a fast model scans every message for potential issues, then a stronger model validates flagged items with full context before interrupting. False positives are dismissed and tracked to prevent re-flagging.

## What it catches

- **Instruction violations** — agent ignoring CLAUDE.md rules, memory, or chat instructions
- **Silent substitution** — agent choosing a different approach without asking
- **Unverified work** — claiming "done" without evidence or testing
- **Code quality issues** — TODOs, placeholders, error swallowing, destructive operations
- **Tool misuse** — fabricated claims, ignored failures, missing tool calls

## Requirements

- Python 3.10+
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` command)
- macOS or Linux (Windows via WSL)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/stefanopochet/pathos/main/install.sh | bash
```

Or clone and install manually:

```bash
git clone https://github.com/stefanopochet/pathos.git
cd pathos
./install.sh
```

## Usage

```bash
pathos                          # start a supervised session
pathos --resume <session-id>    # resume a previous session
PATHOS_DEBUG=1 pathos           # debug mode (5s poll interval)
```

Pathos creates a tmux session, starts Claude Code inside it, and runs a supervisor in the background. You interact with Claude normally — the supervisor watches silently and only interrupts when it finds a real problem.

On exit, it prints a resume command so you can pick up where you left off.

## Configuration

Config lives at `~/.pathos/config.json`:

```json
{
  "triage_model": "claude-haiku-4-5-20251001",
  "validate_model": "claude-opus-4-7",
  "poll_interval": 60,
  "debug_poll_interval": 5
}
```

Custom prompts can be placed in `~/.pathos/prompts/` to override the defaults.

## Updates

Pathos checks for updates on startup and auto-updates when a new version is available.

## License

[Pathos Community License](LICENSE) — free for individuals and companies under $10M revenue. Companies above $10M get 12 months free, then require a commercial license.
