#!/usr/bin/env python3
"""ask.py - read-only Q&A over bot code/logs/data via Groq. Advisory only, never touches trade path."""
import os, sys, subprocess, argparse, datetime, json
import requests
import re

MODEL = os.environ.get("ASK_MODEL", "qwen/qwen3.6-27b")
MAX_CTX_CHARS = 18000

def get_groq_key():
    try:
        from secrets_manager import get_parameter
        return get_parameter('/trading-engine/ai/groq-api-key')
    except Exception as e:
        print("ERROR fetching Groq key from SSM: %s" % e); return None

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout
    except Exception as e:
        return "[cmd failed: %s]" % e

def gather_context(args):
    ctx = []
    logf = args.log or "bot.log"
    if os.path.exists(logf):
        ctx.append("===== %s (last %d lines) =====\n%s" % (logf, args.lines, sh("tail -n %d %s" % (args.lines, logf))))
    if args.grep:
        ctx.append("===== grep '%s' =====\n%s" % (args.grep, sh("grep -nE '%s' %s | tail -n 40" % (args.grep, logf))))
    if args.file and os.path.exists(args.file):
        if args.grep:
            snip = sh("grep -nE -A 25 '%s' %s | head -n 120" % (args.grep, args.file))
        else:
            snip = sh("head -n 120 %s" % args.file)
        ctx.append("===== %s =====\n%s" % (args.file, snip))
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    pend = "candle_archive/pending_%s.json" % today
    if os.path.exists(pend):
        ctx.append("===== %s (head) =====\n%s" % (pend, sh("head -c 1500 %s" % pend)))
    return "\n\n".join(ctx)[:MAX_CTX_CHARS]

def ask_groq(question, context, key):
    system = ("You are a read-only trading-bot analyst. Answer ONLY from the provided context. "
        "If the context lacks the answer, say so. NEVER invent log lines/PnL/trades. NEVER recommend "
        "placing/blocking a live trade - advisory only. Frame strategy ideas as HYPOTHESES to backtest. "
        "Logs are UTC; add +5:30 for IST. Be concise.")
    try:
        resp = requests.post('https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': 'Bearer %s' % key, 'Content-Type': 'application/json'},
            json={'model': MODEL, 'max_tokens': 2000, 'temperature': 0.1,
                  'messages': [{'role': 'system', 'content': system},
                               {'role': 'user', 'content': 'QUESTION:\n%s\n\nCONTEXT:\n%s' % (question, context)}]},
            timeout=60)
        if resp.status_code == 200:
            _ans = next(iter(resp.json()['choices']))['message']['content']
            return re.sub(r'<think>.*?</think>\s*', '', _ans, flags=re.DOTALL).strip()
        return "Groq error: %s - %s" % (resp.status_code, resp.text[:300])
    except Exception as e:
        return "Groq unavailable: %s" % e

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--log", default=None); ap.add_argument("--file", default=None)
    ap.add_argument("--grep", default=None); ap.add_argument("--lines", type=int, default=80)
    args = ap.parse_args()
    key = get_groq_key()
    if not key: print("ERROR: could not load Groq key from SSM."); sys.exit(1)
    ctx = gather_context(args)
    if not ctx.strip(): print("No context found."); sys.exit(1)
    print("=" * 60); print(ask_groq(args.question, ctx, key)); print("=" * 60)
    print("[model=%s | %d chars | read-only]" % (MODEL, len(ctx)))

if __name__ == "__main__":
    main()
