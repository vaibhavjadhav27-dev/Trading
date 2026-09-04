#!/bin/bash
# ============================================================
# MCX V8.5.4 DEPLOYMENT - Mon Aug 24, 2026
# ============================================================
# IMPORTANT: The engine file (mcx_v854_engine.py, 16KB/463 lines)
# is too large for heredoc - must be SCPed from Windows.
# ============================================================

set -euo pipefail
PROD_DIR="/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED"
ROOT_DIR="/home/ubuntu/trading-bot"
BACKUP_DIR="/home/ubuntu/trading-bot/backups/$(date +%Y%m%d_%H%M%S)"
LOG="/home/ubuntu/trading-bot/logs/mcx_v854_deploy.log"

echo "=== MCX V8.5.4 Deployment - $(date) ===" | tee -a "$LOG"

# 1. Create backup
echo "[1/7] Backing up current MCX files..." | tee -a "$LOG"
mkdir -p "$BACKUP_DIR"
cp -v "$PROD_DIR"/mcx_*.py "$BACKUP_DIR/" 2>/dev/null || echo "  (no existing mcx_* files)"
cp -v "$PROD_DIR"/shadow_orchestrator.py "$BACKUP_DIR/" 2>/dev/null || echo "  (no shadow_orchestrator)"

# 2. Verify engine file was SCPed
echo "[2/7] Verifying mcx_v854_engine.py was uploaded..." | tee -a "$LOG"
if [ ! -f "$PROD_DIR/mcx_v854_engine.py" ]; then
    echo "ERROR: mcx_v854_engine.py not found in $PROD_DIR"
    echo "Run from Windows PowerShell first:"
    echo '  scp -i "C:\path\to\key.pem" mcx_v854_engine.py ubuntu@13.207.141.110:/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/'
    exit 1
fi
echo "  [OK] Engine file present ($(wc -l < "$PROD_DIR/mcx_v854_engine.py") lines)"

# 3. Sync to root (PYTHONPATH shadowing fix)
echo "[3/7] Syncing to root dir (PYTHONPATH fix)..." | tee -a "$LOG"
cp -v "$PROD_DIR/mcx_v854_engine.py" "$ROOT_DIR/mcx_v854_engine.py" | tee -a "$LOG"

# 4. Run smoke test
echo "[4/7] Running smoke test..." | tee -a "$LOG"
cd "$PROD_DIR"
python3 -c "from mcx_v854_engine import smoke_test; smoke_test()" 2>&1 | tee -a "$LOG"
if [ $? -ne 0 ]; then
    echo "ERROR: Smoke test FAILED. Restoring backup."
    cp "$BACKUP_DIR"/mcx_v854_engine.py "$PROD_DIR/" 2>/dev/null
    cp "$BACKUP_DIR"/mcx_v854_engine.py "$ROOT_DIR/" 2>/dev/null
    exit 2
fi
echo "  [OK] Smoke test passed" | tee -a "$LOG"

# 5. Fix --close cron bug (if present)
echo "[5/7] Checking cron for --close bug..." | tee -a "$LOG"
CRON_BACKUP="$BACKUP_DIR/crontab_backup.txt"
crontab -l > "$CRON_BACKUP" 2>/dev/null || true
if grep -q "shadow_orchestrator.py --mcx --close" "$CRON_BACKUP"; then
    echo "  Found --close bug; fixing..." | tee -a "$LOG"
    crontab -l | sed 's|shadow_orchestrator.py --mcx --close|shadow_orchestrator.py --report|g' | crontab -
    echo "  [OK] Cron fixed" | tee -a "$LOG"
else
    echo "  No --close bug found (already fixed or different cron)" | tee -a "$LOG"
fi

# 6. Create MCX state directory
echo "[6/7] Ensuring state/log dirs..." | tee -a "$LOG"
mkdir -p "$PROD_DIR/mcx_state"
mkdir -p "$PROD_DIR/logs"

# 7. Verify PYTHONPATH includes both dirs
echo "[7/7] Verifying PYTHONPATH..." | tee -a "$LOG"
echo "  Current PYTHONPATH: ${PYTHONPATH:-'(not set)'}"
# Add to .bashrc if missing
if ! grep -q "V84_PRODUCTION_INTEGRATED" ~/.bashrc; then
    echo 'export PYTHONPATH=/home/ubuntu/trading-bot:/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED' >> ~/.bashrc
    echo "  [OK] Added PYTHONPATH to .bashrc" | tee -a "$LOG"
fi

echo ""
echo "=============================================="
echo " MCX V8.5.4 DEPLOYMENT COMPLETE"
echo "=============================================="
echo " Engine: $PROD_DIR/mcx_v854_engine.py"
echo " Shadow: Will start at MCX open (18:30 IST)"
echo " Backup: $BACKUP_DIR"
echo ""
echo " NEXT: Verify at 18:30 IST tonight:"
echo "   tail -f logs/mcx_v854.log"
echo "   - Contracts resolved correctly?"
echo "   - No 429 rate limit errors?"
echo "   - ORB building from 15x 1-min bars?"
echo "=============================================="
