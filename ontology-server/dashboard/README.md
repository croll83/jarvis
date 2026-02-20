# Ontology Dashboard

Dark Graph / Neo-Observatory knowledge graph explorer built with React 18, styled-components, D3.js.

## Setup

```bash
cd projects/ontology-dashboard
npm install
npm start
```

Opens at http://localhost:3000

## Configuration

- **API URL**: Set `REACT_APP_API_URL` env var (default: `http://127.0.0.1:8100`)
- **Auth**: Enter your Bearer token in the header input
- **Impersonation**: Select a Person from the "Viewing as" dropdown

## Features

- **Entity List** — filterable grid with type sidebar, search, sort
- **Entity Detail** — full property grid + relation tabs
- **Graph View** — D3 force-directed with hover highlight, zoom/pan, relation filters
- **Search** — real-time fuzzy dropdown + full search page
- **Auth** — Bearer token with localStorage persistence
- **Impersonation** — X-Speaker-Id header, speaker selector

## Production Build

```bash
npm run build
```

Output in `build/` — can be served as static files by ontology-server.

## Tech Stack

- React 18 + React Router 6
- styled-components (Dark Graph theme)
- D3.js (force-directed graph)
- Axios (API client with auth interceptors)
