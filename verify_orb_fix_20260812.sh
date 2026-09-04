#!/bin/bash
# ORB cap-fix live verification � run on Wed Aug 12 after the ORB window (>= 04:15 UTC / 09:45 IST)
cd ~/trading-bot
LOG=bot.log
DATE_TODAY=$(date -u +%Y-%m-%d)

echo "###### ORB CAP FIX � LIVE VERIFICATION ($DATE_TODAY) ######"
echo ""
echo "--- baseline (Tue Aug 11, OLD [:8] cap) ---"
echo "    ORB recorded: 5 valid ranges | 3 range-outside | pool=8"
echo ""
echo "--- TODAY's live ORB result (NEW [:35] cap) ---"
grep "ORB recorded:" $LOG | tail -1
grep "ORB breakdown:" $LOG | tail -1
grep "Recording ORB for" $LOG | tail -1
echo ""
echo "--- did the bot SURVIVE an L=None moment (Bug A fix)? ---"
grep -E "SIDE SELECT:|SCORE RESOLVE:" $LOG | tail -5
echo ""
echo "--- any _side UnboundLocalError today? (should be NONE) ---"
grep -c "UnboundLocalError.*_side" $LOG | xargs -I{} echo "    _side crashes today: {}  (expect 0)"
echo ""
echo "--- gate confirmation still 108? ---"
venv/bin/python3 -c "import short_live as s; print('    GATE:', s.MIN_SCORE, 'CONF%:', s.CONFIDENCE_PCT)" 2>/dev/null
echo ""
echo "--- did any trade actually fire? ---"
grep -iE "ENTRY|ORDER PLACED|BUY placed|SELL placed|trade taken" $LOG | tail -10
echo ""
echo "###### VERDICT ######"
VALID=$(grep "ORB recorded:" $LOG | tail -1 | grep -oE "[0-9]+ valid" | grep -oE "[0-9]+")
if [ -n "$VALID" ]; then
    echo "    Valid ORB ranges today: $VALID  (baseline was 5 from [:8])"
    if [ "$VALID" -gt 5 ]; then
        echo "    ==> FIX CONFIRMED LIVE: pool widened, more valid ranges than [:8] baseline"
    else
        echo "    ==> Valid ranges NOT higher � investigate (thin market? or 35-fetch hit rate limit?)"
    fi
fi
