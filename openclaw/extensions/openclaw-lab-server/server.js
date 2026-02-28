const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8003;
const ROOT = '/projects';
const MIME = {
  '.html':'text/html','.css':'text/css','.js':'application/javascript',
  '.json':'application/json','.jsonl':'application/x-ndjson','.png':'image/png','.jpg':'image/jpeg',
  '.svg':'image/svg+xml','.ico':'image/x-icon','.woff2':'font/woff2',
  '.woff':'font/woff','.ttf':'font/ttf','.map':'application/json',
};

function listProjects() {
  try {
    return fs.readdirSync(ROOT).filter(d =>
      fs.statSync(path.join(ROOT, d)).isDirectory() && d !== 'openclaw-lab-server'
    );
  } catch { return []; }
}

function serve(res, filePath, fallback) {
  if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
    const ext = path.extname(filePath);
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
    fs.createReadStream(filePath).pipe(res);
  } else if (fallback && fs.existsSync(fallback)) {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    fs.createReadStream(fallback).pipe(res);
  } else {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('404 Not Found');
  }
}

http.createServer((req, res) => {
  const url = decodeURIComponent(req.url.split('?')[0]);

  if (url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ status: 'ok', projects: listProjects() }));
  }

  if (url === '/') {
    const projects = listProjects();
    res.writeHead(200, { 'Content-Type': 'text/html' });
    return res.end(`<h1>openclaw-lab-server</h1><ul>${projects.map(p =>
      `<li><a href="/${p}/">${p}</a></li>`).join('')}</ul>`);
  }

  // Serve routing-log.jsonl (single file mount)
  if (url === '/routing-log.jsonl') {
    return serve(res, '/app/routing-log.jsonl');
  }

  const parts = url.replace(/^\//, '').split('/');
  const project = parts[0];
  const rest = parts.slice(1).join('/') || 'index.html';
  const projectDir = path.join(ROOT, project);
  if (!fs.existsSync(projectDir) || !fs.statSync(projectDir).isDirectory()) {
    res.writeHead(404); return res.end('Project not found');
  }
  const filePath = path.join(projectDir, rest);
  const indexFallback = path.join(projectDir, 'index.html');
  // Prevent path traversal
  if (!filePath.startsWith(projectDir)) { res.writeHead(403); return res.end('Forbidden'); }
  serve(res, filePath, indexFallback);
}).listen(PORT, '0.0.0.0', () => console.log(`Lab server on :${PORT}`));
