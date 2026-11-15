# shellcheck shell=bash
# Ubuntu Server (22.04 and newer). See lib/os.sh for what each function is for.

os_archive_url() { echo "https://archive.ubuntu.com"; }

os_docker_repo_url() { echo "https://download.docker.com/linux/ubuntu"; }

os_extra_packages() { echo "apt-transport-https"; }

os_unattended_origins() {
    cat << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
EOF
}
