'use strict';

const express = require('express');
const http    = require('http');
const path    = require('path');
const pty     = require('node-pty');
const { WebSocketServer } = require('ws');

const app    = express();
const server = http.createServer(app);
const wss    = new WebSocketServer({ server });

// ── Runtime config ────────────────────────────────────────────────────────────
// CONTROL_PLANE_HOST is set by setup.py (defaults to localhost for local use).
const host         = process.env.CONTROL_PLANE_HOST || 'localhost';
const giteaPort    = process.env.GITEA_PORT          || '30080';
const argoPort     = process.env.ARGOCD_PORT         || '8080';
const demoRepo     = process.env.DEMO_REPO           || 'admin/demo-repo';

app.get('/config', (_req, res) => {
  res.json({
    giteaUrl: `http://${host}:${giteaPort}`,
    argoUrl:  `http://${host}:${argoPort}`,
    demoRepo,
  });
});

// Serve static files (index.html, node_modules, etc.)
app.use(express.static(path.join(__dirname)));

wss.on('connection', (ws) => {
  // SHELL_CMD lets us break out to the host via nsenter in privileged pods.
  // e.g. SHELL_CMD="nsenter -t 1 -m -u -i -n -p -- /bin/bash"
  // Without it, falls back to a local /bin/bash.
  const shellCmd = process.env.SHELL_CMD
    ? process.env.SHELL_CMD.trim().split(/\s+/)
    : [process.env.SHELL || '/bin/bash', '--login'];
  const [shellBin, ...shellArgs] = shellCmd;
  const ptyProcess = pty.spawn(shellBin, shellArgs, {
    name: 'xterm-256color',
    cols: 80,
    rows: 24,
    cwd: process.env.HOME || '/',
    env: process.env,
  });

  // PTY → browser
  ptyProcess.onData((data) => {
    if (ws.readyState === ws.OPEN) ws.send(data);
  });

  // Browser → PTY
  ws.on('message', (msg) => {
    const text = Buffer.isBuffer(msg) ? msg.toString('utf8') : msg;
    try {
      const obj = JSON.parse(text);
      if (obj.type === 'resize') {
        ptyProcess.resize(
          Math.max(2, Math.min(500, obj.cols)),
          Math.max(1, Math.min(200, obj.rows))
        );
        return;
      }
      if (obj.type === 'b64') {
        ptyProcess.write(Buffer.from(obj.data, 'base64').toString('utf8'));
        return;
      }
    } catch (_) { /* raw input */ }
    ptyProcess.write(text);
  });

  ws.on('close', () => ptyProcess.kill());
  ws.on('error', () => ptyProcess.kill());
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, '0.0.0.0', () => {
  console.log(`GitOps Workshop → http://${host}:${PORT}`);
});
