# shellcheck shell=bash
# System packages, Docker Engine and Tailscale. What differs between Ubuntu and Debian
# comes from lib/os/*.sh; what OpenMediaVault manages itself from lib/platform.sh.

install_packages() {
    local -a packages=(
        ca-certificates curl gnupg lsb-release jq net-tools git smartmontools
        htop iotop ncdu vim tmux
    )
    local -a extra
    os_require
    # what only this distribution needs (lib/os/*.sh)
    IFS=" " read -ra extra <<< "$(os_extra_packages)"
    packages+=(${extra[@]+"${extra[@]}"})
    # OpenMediaVault has its own firewall and Update Management (lib/platform.sh)
    platform_is_omv || packages+=(unattended-upgrades ufw)

    info "Updating package lists..."
    apt-get update -qq

    info "Installing core packages..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"

    if platform_is_omv; then
        info "OpenMediaVault: automatic updates left to OMV (System > Update Management)"
    else
        configure_unattended_upgrades
    fi

    success "Core packages installed"
}

configure_unattended_upgrades() {
    info "Configuring unattended-upgrades..."
    {
        os_unattended_origins
        cat << 'EOF'
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::SyslogEnable "true";
EOF
    } > "$APT_CONF_DIR/50unattended-upgrades"

    cat > "$APT_CONF_DIR/20auto-upgrades" << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

    systemctl enable unattended-upgrades
    systemctl start unattended-upgrades
}

install_docker() {
    if command -v docker &> /dev/null; then
        info "Docker already installed, checking version..."
        docker --version
    else
        os_require
        local repo
        repo=$(os_docker_repo_url)
        info "Installing Docker Engine from $repo..."

        # Add Docker's official GPG key
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "$repo/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg

        # Add Docker repository
        echo \
            "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] $repo \
            $OS_CODENAME stable" | \
            tee /etc/apt/sources.list.d/docker.list > /dev/null

        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi

    # Add current sudo user to docker group
    local real_user="${SUDO_USER:-$USER}"
    if [[ -n "$real_user" ]] && [[ "$real_user" != "root" ]]; then
        usermod -aG docker "$real_user" 2>/dev/null || true
        info "Added $real_user to docker group"
    fi

    systemctl enable docker
    systemctl start docker

    success "Docker installed and configured"
}

install_tailscale() {
    if command -v tailscale &> /dev/null; then
        info "Tailscale already installed"
    else
        info "Installing Tailscale..."
        curl -fsSL https://tailscale.com/install.sh | sh
    fi

    systemctl enable tailscaled
    systemctl start tailscaled

    # Authenticate if key provided
    if [[ -n "$TAILSCALE_AUTH_KEY" ]]; then
        info "Authenticating Tailscale..."
        tailscale up --authkey="$TAILSCALE_AUTH_KEY" --accept-routes --accept-dns=false || true
    else
        warn "Tailscale installed but not authenticated. Run: sudo tailscale up"
    fi

    success "Tailscale configured"
}
