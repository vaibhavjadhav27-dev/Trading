# Install swing-monitor as a systemd service (auto-restart on crash)
sudo cp swing-monitor.service /etc/systemd/system/swing-monitor.service
sudo mkdir -p /home/ubuntu/trading-bot/swing_logs
sudo systemctl daemon-reload
sudo systemctl enable swing-monitor.service
sudo systemctl start swing-monitor.service
# check:
sudo systemctl status swing-monitor.service
journalctl -u swing-monitor.service -f
