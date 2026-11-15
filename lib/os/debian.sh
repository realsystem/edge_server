# shellcheck shell=bash
# Debian 12 (bookworm) and newer, OpenMediaVault included (it is Debian underneath).
# See lib/os.sh for what each function is for.

os_archive_url() { echo "https://deb.debian.org"; }

os_docker_repo_url() { echo "https://download.docker.com/linux/debian"; }

# apt-transport-https is an empty transitional package on Debian (https is built into apt)
os_extra_packages() { :; }

# Debian's own default: the release and its security updates, nothing else
os_unattended_origins() {
    cat << 'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian";
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};
EOF
}
