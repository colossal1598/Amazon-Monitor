/**
 * PM2: Amazon monitor + WhatsApp server + periodic healthcheck.
 * From this folder: start-pm2-stack.bat  OR  pm2 start ecosystem.config.cjs
 *
 * wa-server path: sibling of "Amazon Scraper" -> ../../wa-server from amazon_monitor.
 * Override: set WA_SERVER_ROOT before pm2 start.
 */
const path = require("path");

const monitorRoot = __dirname;
const waServerRoot = process.env.WA_SERVER_ROOT || path.join(monitorRoot, "..", "..", "wa-server");
const monitorPython = path.join(monitorRoot, ".venv", "Scripts", "python.exe");

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
    },
    {
      name: "wa-server",
      cwd: waServerRoot,
      script: "server.js",
      autorestart: true,
      max_restarts: 20,
      exp_backoff_restart_delay: 3000,
      watch: false,
      // Soft restart every 3h (helps Node / whatsapp-web.js memory). Auth stays in .wwebjs_auth.
      cron_restart: "0 */3 * * *",
    },
    {
      name: "monitor-healthcheck",
      cwd: monitorRoot,
      script: "tools/healthcheck.py",
      interpreter: monitorPython,
      autorestart: false,
      watch: false,
      // Run check every 10 minutes (process exits; PM2 starts again on schedule).
      cron_restart: "*/10 * * * *",
    },
  ],
};
