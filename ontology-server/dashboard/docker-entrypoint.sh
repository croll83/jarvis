#!/bin/sh
# Generate runtime config from environment variables
cat <<EOF > /usr/share/nginx/html/env-config.js
window.__RUNTIME_CONFIG__ = {
  API_URL: "${API_URL:-http://127.0.0.1:8100}"
};
EOF

echo "Dashboard configured with API_URL=${API_URL:-http://127.0.0.1:8100}"
exec "$@"
