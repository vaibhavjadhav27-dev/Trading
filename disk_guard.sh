#!/bin/bash
USE=$(df --output=pcent / | tr -dc '0-9')
if [ "$USE" -ge 85 ]; then
  echo "$(date) DISK ALERT: root at ${USE}%" >> /home/ubuntu/trading-bot/logs/disk_guard.log
  sudo rm -rf /var/lib/snapd/cache/* 2>/dev/null
  sudo apt-get clean 2>/dev/null
fi
