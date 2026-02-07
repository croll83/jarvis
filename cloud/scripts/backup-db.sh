#!/bin/bash
# =============================================================================
# JARVIS Database Backup Script
# =============================================================================
# Creates a timestamped backup of the JARVIS SQLite database
#
# Usage:
#   ./backup-db.sh              # Backup to default location
#   ./backup-db.sh /path/to/dir # Backup to specific directory
#
# Recommended: Add to crontab for daily backups
#   0 3 * * * /opt/jarvis/cloud/scripts/backup-db.sh >> /var/log/jarvis-backup.log 2>&1

set -e

# Configuration
JARVIS_DATA_DIR="/opt/jarvis/data"
DB_FILE="$JARVIS_DATA_DIR/jarvis.db"
BACKUP_DIR="${1:-/opt/jarvis/backups}"
RETENTION_DAYS=30

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/jarvis_$TIMESTAMP.db"

echo -e "${YELLOW}[$(date)] Starting JARVIS database backup...${NC}"

# Check if database exists
if [ ! -f "$DB_FILE" ]; then
    echo -e "${RED}Error: Database not found at $DB_FILE${NC}"
    exit 1
fi

# Create backup directory if needed
mkdir -p "$BACKUP_DIR"

# Create backup using SQLite's .backup command (safe for live databases)
echo "Creating backup..."
sqlite3 "$DB_FILE" ".backup '$BACKUP_FILE'"

# Compress the backup
echo "Compressing backup..."
gzip "$BACKUP_FILE"
BACKUP_FILE="${BACKUP_FILE}.gz"

# Get file size
SIZE=$(du -h "$BACKUP_FILE" | cut -f1)

echo -e "${GREEN}Backup created: $BACKUP_FILE ($SIZE)${NC}"

# Clean old backups
echo "Cleaning backups older than $RETENTION_DAYS days..."
DELETED=$(find "$BACKUP_DIR" -name "jarvis_*.db.gz" -mtime +$RETENTION_DAYS -delete -print | wc -l)
echo "Deleted $DELETED old backup(s)"

# List recent backups
echo ""
echo "Recent backups:"
ls -lh "$BACKUP_DIR"/jarvis_*.db.gz 2>/dev/null | tail -5

echo ""
echo -e "${GREEN}[$(date)] Backup complete!${NC}"
