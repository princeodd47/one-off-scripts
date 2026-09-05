#!/usr/bin/env node
/**
 * Claude Code status line script.
 * Reads the status-line JSON payload from stdin and prints a single
 * summary line to stdout.
 */

const { execSync } = require("child_process");
const os = require("os");

function readStdin() {
  try {
    const fs = require("fs");
    return fs.readFileSync(0, "utf8");
  } catch (e) {
    return "";
  }
}

function shortenPath(p) {
  if (!p) return "";
  const home = os.homedir();
  let display = p;
  if (home && p.toLowerCase().startsWith(home.toLowerCase())) {
    display = "~" + p.slice(home.length);
  }
  const sep = display.includes("/") ? "/" : "\\";
  const parts = display.split(/[\\/]+/).filter(Boolean);
  const hasTilde = display.startsWith("~");
  if (parts.length > 3) {
    const tail = parts.slice(-2);
    display = (hasTilde ? "~" : "") + sep + "..." + sep + tail.join(sep);
  }
  return display;
}

function getGitBranch(cwd) {
  try {
    const branch = execSync(
      "git --no-optional-locks rev-parse --abbrev-ref HEAD",
      { cwd, stdio: ["ignore", "pipe", "ignore"] }
    )
      .toString()
      .trim();
    if (!branch || branch === "HEAD") {
      // Detached HEAD: fall back to short commit hash.
      try {
        const sha = execSync("git --no-optional-locks rev-parse --short HEAD", {
          cwd,
          stdio: ["ignore", "pipe", "ignore"],
        })
          .toString()
          .trim();
        return sha ? `detached@${sha}` : null;
      } catch (e) {
        return null;
      }
    }
    return branch;
  } catch (e) {
    return null;
  }
}

function getGitDirtyCounts(cwd) {
  try {
    const staged = execSync("git --no-optional-locks diff --cached --numstat", {
      cwd,
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
    const modified = execSync("git --no-optional-locks diff --numstat", {
      cwd,
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
    const stagedCount = staged ? staged.split("\n").length : 0;
    const modifiedCount = modified ? modified.split("\n").length : 0;
    return { stagedCount, modifiedCount };
  } catch (e) {
    return { stagedCount: 0, modifiedCount: 0 };
  }
}

const RESET = "\x1b[0m";
const DIM = "\x1b[2m";
const COLOR = {
  model: "\x1b[38;5;141m", // purple
  cwd: "\x1b[38;5;110m", // blue
  branch: "\x1b[38;5;108m", // green
  staged: "\x1b[38;5;108m", // green
  modified: "\x1b[38;5;179m", // yellow
  green: "\x1b[38;5;107m",
  yellow: "\x1b[38;5;179m",
  red: "\x1b[38;5;167m",
};

function usageColor(pct) {
  if (pct >= 80) return COLOR.red;
  if (pct >= 50) return COLOR.yellow;
  return COLOR.green;
}

function usageBar(pct, width = 10) {
  const filled = Math.round((pct / 100) * width);
  const bar = "█".repeat(filled) + "░".repeat(width - filled);
  const color = usageColor(pct);
  return `${color}${bar}${RESET} ${color}${Math.round(pct)}%${RESET}`;
}

function labeledBar(label, pct, width = 5) {
  const clamped = Math.max(0, Math.min(100, pct));
  const filled = Math.round((clamped / 100) * width);
  const bar = "█".repeat(filled) + "░".repeat(width - filled);
  const color = usageColor(pct);
  return `${label} ${color}${bar}${RESET} ${color}${Math.round(pct)}%${RESET}`;
}

function main() {
  const raw = readStdin();
  let input = {};
  try {
    input = JSON.parse(raw);
  } catch (e) {
    input = {};
  }

  const parts = [];

  // Model
  const modelName =
    (input.model && (input.model.display_name || input.model.id)) || "Claude";
  parts.push(`${COLOR.model}${modelName}${RESET}`);

  // Working directory
  const cwd =
    (input.workspace && input.workspace.current_dir) || input.cwd || process.cwd();
  const shortCwd = shortenPath(cwd);
  if (shortCwd) parts.push(`${COLOR.cwd}${shortCwd}${RESET}`);

  // Git branch + dirty-state counts
  const branch = getGitBranch(cwd);
  if (branch) {
    const { stagedCount, modifiedCount } = getGitDirtyCounts(cwd);
    let branchPart = `${COLOR.branch}(${branch})${RESET}`;
    if (stagedCount > 0) branchPart += ` ${COLOR.staged}+${stagedCount}${RESET}`;
    if (modifiedCount > 0) branchPart += ` ${COLOR.modified}~${modifiedCount}${RESET}`;
    parts.push(branchPart);
  }

  // Context window usage as a colored bar
  const ctx = input.context_window;
  if (ctx && typeof ctx.used_percentage === "number") {
    parts.push(usageBar(ctx.used_percentage));
  }

  // Rate limit usage as compact bars (Pro/Max subscribers only; absent otherwise)
  const rateLimits = input.rate_limits;
  if (rateLimits) {
    const fiveHour = rateLimits.five_hour?.used_percentage;
    const sevenDay = rateLimits.seven_day?.used_percentage;
    if (typeof fiveHour === "number") parts.push(labeledBar("5h", fiveHour));
    if (typeof sevenDay === "number") parts.push(labeledBar("7d", sevenDay));
  }

  process.stdout.write(parts.join(`${DIM} | ${RESET}`));
}

main();
