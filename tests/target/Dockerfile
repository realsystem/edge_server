FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV container=docker

# Install systemd and SSH server
RUN apt-get update && apt-get install -y \
    systemd \
    systemd-sysv \
    openssh-server \
    sudo \
    curl \
    wget \
    ca-certificates \
    gnupg \
    lsb-release \
    iproute2 \
    procps \
    vim \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI only (uses host Docker via socket)
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Create docker group and test user with sudo/docker access
RUN groupadd -g 999 docker || true \
    && useradd -m -s /bin/bash testuser \
    && echo "testuser:testpass" | chpasswd \
    && usermod -aG sudo,docker testuser \
    && echo "testuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/testuser

# Setup SSH
RUN mkdir -p /var/run/sshd \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && systemctl enable ssh

# Create .ssh directory for testuser
RUN mkdir -p /home/testuser/.ssh \
    && chmod 700 /home/testuser/.ssh \
    && chown -R testuser:testuser /home/testuser/.ssh

# Create directories that deploy scripts expect
RUN mkdir -p /opt/edge-server /var/log /mnt/storage

# Create a docker.service that symlinks the mounted socket and fixes permissions
RUN mkdir -p /etc/systemd/system && \
    printf '[Unit]\nDescription=Docker (host socket)\nConditionPathExists=/docker.sock\n\n[Service]\nType=oneshot\nExecStart=/bin/bash -c "ln -sf /docker.sock /var/run/docker.sock && chmod 666 /docker.sock"\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n' > /etc/systemd/system/docker.service

# Remove unnecessary systemd services that won't work in container
RUN rm -f /lib/systemd/system/multi-user.target.wants/* \
    /etc/systemd/system/*.wants/* \
    /lib/systemd/system/local-fs.target.wants/* \
    /lib/systemd/system/sockets.target.wants/*udev* \
    /lib/systemd/system/sockets.target.wants/*initctl* \
    /lib/systemd/system/sysinit.target.wants/systemd-tmpfiles-setup* \
    /lib/systemd/system/systemd-update-utmp* \
    2>/dev/null || true

# Re-enable ssh and our fake docker service after cleanup
RUN systemctl enable ssh && systemctl enable docker

VOLUME ["/sys/fs/cgroup"]

STOPSIGNAL SIGRTMIN+3

EXPOSE 22

CMD ["/lib/systemd/systemd"]
