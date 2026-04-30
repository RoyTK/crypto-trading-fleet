#!/usr/bin/env bash
# Phase 0 post-provision setup for the Hetzner box.
# Run this ONCE as root immediately after a fresh Ubuntu 24.04 install.
# Idempotent â€” safe to re-run.
set -euo pipefail

echo "[1/10] System update..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

echo "[2/10] Install base utilities..."
apt-get install -y \
  ufw fail2ban unattended-upgrades \
  ca-certificates curl gnupg lsb-release \
  htop tmux git rsync jq net-tools

echo "[3/10] Auto-security-upgrades..."
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'AUTO'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
AUTO

echo "[4/10] Create 'fleet' user (non-root)..."
if ! id fleet >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G sudo fleet
fi
mkdir -p /home/fleet/.ssh
cp /root/.ssh/authorized_keys /home/fleet/.ssh/authorized_keys
chown -R fleet:fleet /home/fleet/.ssh
chmod 700 /home/fleet/.ssh
chmod 600 /home/fleet/.ssh/authorized_keys
echo 'fleet ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/fleet
chmod 440 /etc/sudoers.d/fleet

echo "[5/10] SSH hardening..."
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
systemctl restart ssh || systemctl restart sshd

echo "[6/10] UFW firewall (deny inbound except 22)..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

echo "[7/10] fail2ban..."
systemctl enable --now fail2ban

echo "[8/10] Install Docker..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker fleet

echo "[9/10] Add 4 GB swap..."
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=10' >> /etc/sysctl.conf
  sysctl -p
fi

echo "[10/10] UTC timezone + project dir..."
timedatectl set-timezone UTC
mkdir -p /home/fleet/crypto-fleet
chown -R fleet:fleet /home/fleet/crypto-fleet

echo ""
echo "======================================"
echo "Setup complete."
echo "Root SSH is now DISABLED."
echo "Reconnect as: ssh fleet@<server-ip>"
echo "Update ~/.ssh/config: change 'User root' to 'User fleet'"
echo "======================================"
