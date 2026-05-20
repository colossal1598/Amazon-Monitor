/**
 * PM2: Amazon monitor + admin UI + WhatsApp server + healthcheck.
 * From this folder: start-pm2-stack.bat  OR  pm2 start ecosystem.config.cjs
 *
 * wa-server path: sibling of "Amazon Scraper" -> ../../wa-server from amazon_monitor.
 * Override: set WA_SERVER_ROOT before pm2 start.
 */
const fs = require("fs");
const path = require("path");

const monitorRoot = __dirname;
const waServerRoot = process.env.WA_SERVER_ROOT || path.join(monitorRoot, "..", "..", "wa-server");
const monitorPython = path.join(monitorRoot, ".venv", "Scripts", "python.exe");

/** Load amazon_monitor/.env into a plain object for PM2 (so admin-ui gets credentials). */
function loadDotEnv(filePath) {
  const out = {};
  if (!fs.existsSync(filePath)) {
    return out;
  }
  const text = fs.readFileSync(filePath, "utf8");
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const eq = trimmed.indexOf("=");
    if (eq <= 0) {
      continue;
    }
    const key = trimmed.slice(0, eq).trim();
    let val = trimmed.slice(eq + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  return out;
}

const dotEnv = loadDotEnv(path.join(monitorRoot, ".env"));
const adminUiEnv = {
  ...dotEnv,
  PYTHONUNBUFFERED: "1",
};

module.exports = {
  apps: [
    {
      name: "amazon-monitor",
      cwd: monitorRoot,
      script: "main.py",
      interpreter: monitorPython,
      autorestart: true,
      max_restarts: 20,
      exp_backoff_restart_delay: 3000,
      watch: false,
      env: {
        PYTHONUNBUFFERED: "1",
        ...dotEnv,
      },
    },
    {
      name: "admin-ui",
      cwd: monitorRoot,
      script: "tools/admin_ui_server.py",
      interpreter: monitorPython,
      autorestart: true,
      max_restarts: 20,
      exp_backoff_restart_delay: 3000,
      watch: false,
      env: adminUiEnv,
      error_file: path.join(monitorRoot, "logs", "admin-ui.err.log"),
      out_file: path.join(monitorRoot, "logs", "admin-ui.out.log"),
      merge_logs: true,
    },
    {
      name: "wa-server",
      cwd: waServerRoot,
      script: "server.js",
      autorestart: true,
      max_restarts: 20,
      exp_backoff_restart_delay: 3000,
      watch: false,
      cron_restart: "0 */3 * * *",
    },
    {
      name: "monitor-healthcheck",
      cwd: monitorRoot,
      script: "tools/healthcheck.py",
      interpreter: monitorPython,
      autorestart: false,
      watch: false,
      cron_restart: "*/10 * * * *",
    },
  ],
};
