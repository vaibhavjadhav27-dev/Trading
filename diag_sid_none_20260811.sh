#!/usr/bin/env bash
# ============================================================================
# diag_sid_none_20260811.sh � find WHY lc_sid/shc_sid resolve to None
# Run on the server:  bash diag_sid_none_20260811.sh
# ============================================================================
cd ~/trading-bot || exit 1

echo "###### B1 � where are lc_sid / shc_sid assigned? ######"
grep -n "lc_sid\|shc_sid\|SCORE RESOLVE" trading_bot.py | head -30

echo ""
echo "###### B2 � the SID lookup source (how a ticker -> security_id) ######"
grep -n "security_id\|sid_map\|SID\b\|get_sid\|instrument.*id\|SECURITY_ID" trading_bot.py | head -30

echo ""
echo "###### B3 � is the instrument/SID master file present & fresh? ######"
ls -la *.csv *.json 2>/dev/null | grep -iE "instrument|master|sid|scrip|symbol" 
echo "--- any SID file modified today? ---"
find . -maxdepth 2 -newermt "2026-08-11 00:00" \( -name "*instrument*" -o -name "*master*" -o -name "*sid*" -o -name "*scrip*" \) 2>/dev/null

echo ""
echo "###### B4 � today's log: what happened right before L=None? ######"
grep -nB3 "SCORE RESOLVE: L=None" bot.log 2>/dev/null | tail -25
grep -niE "sid.*none|no sid|resolve.*fail|instrument.*not|lookup.*fail|KeyError" bot.log 2>/dev/null | tail -20

echo ""
echo "###### B5 � the two long/short CANDIDATE picks whose SID went None ######"
# lc = long candidate, shc = short candidate � which tickers were selected as #1?
grep -niE "top long|top short|#1|rank.?1|lc=|shc=|best long|best short|selected long|selected short" bot.log 2>/dev/null | tail -20
