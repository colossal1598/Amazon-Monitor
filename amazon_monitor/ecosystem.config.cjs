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

module.exports = {
  apps: [
    {
      name: "amazon-monitor",
      cwd: monitorRoot,
      script: "main.py",
      interpreter: "C:\\amazon-monitor\\amazon_monitor\\.venv\\Scripts\\python.exe",
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
      interpreter: "python",
      autorestart: false,
      watch: false,
      // Run check every 10 minutes (process exits; PM2 starts again on schedule).
      cron_restart: "*/10 * * * *",
    },
  ],
};
