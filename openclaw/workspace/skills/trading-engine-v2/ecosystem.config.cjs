module.exports = {
  apps: [{
    name: "trading-v2",
    script: "daemon/market-daemon.mjs",
    interpreter: "node",
    cwd: "/home/jarvis/.openclaw/workspace/skills/trading-engine-v2",
    log_file: "state/daemon-pm2.log",
    time: true,
    env: {
      DRY_RUN: "false",
    },
  }],
};
