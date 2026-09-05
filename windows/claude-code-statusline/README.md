# claude-code-statusline

A status line script for [Claude Code](https://claude.com/claude-code) that shows the
current model, working directory, git branch with dirty-state counts, a colored
context-usage bar, and rate-limit usage.

Example output:
`Claude Sonnet 5 | ~/git/one-off-scripts | (master) +1 ~2 | ██████░░░░ 62% | 5h ███░░ 24% | 7d █████░ 41%`

- `+N` staged files (green), `~N` modified files (yellow), next to the branch name
- All bars are green under 50%, yellow 50-79%, red 80%+
- The `5h`/`7d` rate-limit bars only appear for Claude.ai Pro/Max subscribers, once
  the session has made its first API call

## Setup

Add to `~/.claude/settings.json` (Windows: `C:\Users\<you>\.claude\settings.json`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "node C:/git/one-off-scripts/windows/claude-code-statusline/statusline.js"
  }
}
```

Requires Node.js (already bundled with Claude Code's runtime) and, optionally, a
terminal font with block-character (`█`/`░`) support.
