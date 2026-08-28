# Edge Server - Local Testing
#
# Usage:
#   make setup       - Create Python venv and install dependencies
#   make test        - Start lightweight test stack (1 camera)
#   make test-full   - Start full test stack (4 cameras)
#   make stop        - Stop all containers
#   make clean       - Stop and remove all data

VENV_DIR := .venv
PYTHON := $(VENV_DIR)/bin/python
PIP := $(VENV_DIR)/bin/pip
TESTS_DIR := tests
LITE_COMPOSE := $(TESTS_DIR)/docker-compose.lite.yml
FULL_COMPOSE := $(TESTS_DIR)/docker-compose.mac.yml
HARNESS_COMPOSE := $(TESTS_DIR)/docker-compose.test-harness.yml

.PHONY: help setup pytest check check-mem-lite check-mem-full test test-dev test-ci test-full test-full-dev test-scripts start start-full stop status logs clean reset lint mem target-start target-stop target-ssh victron-setup victron-check victron-scan victron-install victron-test nut-install nut-status nut-uninstall

help:
	@echo "Edge Server - Local Testing"
	@echo ""
	@echo "Targets:"
	@echo "  make setup          - Create Python venv and install dependencies"
	@echo "  make pytest         - Run Python unit tests"
	@echo "  make test           - Run lite tests with pass/fail (auto-teardown)"
	@echo "  make test-dev       - Lite tests, leave stack running"
	@echo "  make test-full      - Run full tests (4 cameras, HA, auto-teardown)"
	@echo "  make test-full-dev  - Full tests, leave stack running"
	@echo "  make start          - Start lite stack without tests"
	@echo "  make start-full     - Start full stack without tests"
	@echo "  make test-scripts   - Run shell script unit tests"
	@echo "  make stop           - Stop all containers"
	@echo "  make status         - Show container status"
	@echo "  make logs           - Follow logs (Ctrl+C to exit)"
	@echo "  make clean          - Stop and remove all test data"
	@echo "  make lint           - Check shell scripts with shellcheck"
	@echo "  make mem            - Show memory usage of running containers"
	@echo ""
	@echo "Test Target (deployment testing without real hardware):"
	@echo "  make target-start   - Start Ubuntu container with SSH (port 2222)"
	@echo "  make target-stop    - Stop test target container"
	@echo "  make target-ssh     - SSH into test target"
	@echo ""
	@echo "Victron Smart Shunt (BLE battery monitor):"
	@echo "  make victron-setup   - Install victron-shunt CLI tool"
	@echo "  make victron-check   - Check if Bluetooth is available"
	@echo "  make victron-scan    - Scan for Victron BLE devices"
	@echo "  make victron-test    - Run victron-shunt unit tests"
	@echo "  make victron-install - Install systemd service (requires sudo)"
	@echo ""
	@echo "NUT Power Monitor (laptop battery shutdown):"
	@echo "  make nut-install     - Install NUT power monitor (requires sudo)"
	@echo "  make nut-status      - Show battery/UPS status"
	@echo "  make nut-uninstall   - Remove NUT power monitor"
	@echo ""
	@echo "Memory Requirements:"
	@echo "  Lite test:  ~1.3 GB (1 camera)"
	@echo "  Full test:  ~3.2 GB (4 cameras + Home Assistant)"
	@echo ""
	@echo "Access (after start):"
	@echo "  Frigate:  http://localhost:5000"
	@echo "  MQTT:     localhost:1883"

# Python setup - finds Python 3.8+
PYTHON3 := $(shell for cmd in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do \
	if command -v $$cmd >/dev/null 2>&1; then \
		ver=$$($$cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null); \
		major=$$(echo $$ver | cut -d. -f1); \
		minor=$$(echo $$ver | cut -d. -f2); \
		if [ "$$major" -ge 3 ] && [ "$$minor" -ge 8 ]; then \
			echo $$cmd; \
			break; \
		fi; \
	fi; \
done)

setup:
	@if [ -z "$(PYTHON3)" ]; then \
		echo "ERROR: Python 3.8+ required"; \
		exit 1; \
	fi
	@echo "Using $(PYTHON3)..."
	@rm -rf $(VENV_DIR)
	@echo "Creating Python virtual environment..."
	@$(PYTHON3) -m venv $(VENV_DIR)
	@$(PIP) install --upgrade pip setuptools
	@$(PIP) install -e ".[dev]"
	@echo "Done. Activate with: source $(VENV_DIR)/bin/activate"

pytest: $(VENV_DIR)
	@$(PYTHON) -m pytest tests/ -v

$(VENV_DIR):
	@$(MAKE) setup

check:
	@docker info >/dev/null 2>&1 || (echo "ERROR: Docker not running" && exit 1)
	@echo "Docker OK"

# Check memory requirements (in MB)
LITE_MEM_REQUIRED := 1500
FULL_MEM_REQUIRED := 3500

check-mem-lite: check
	@echo "Checking memory for lite test (requires $(LITE_MEM_REQUIRED) MB)..."
	@mem=$$(docker info --format '{{.MemTotal}}' 2>/dev/null); \
	if [ -z "$$mem" ] || [ "$$mem" = "0" ]; then \
		echo "WARN: Could not detect Docker memory limit, skipping check"; \
	else \
		mem_mb=$$(echo "$$mem" | awk '{print int($$1/1024/1024)}'); \
		echo "Docker memory limit: $${mem_mb} MB"; \
		if [ "$${mem_mb}" -lt "$(LITE_MEM_REQUIRED)" ]; then \
			echo "ERROR: Insufficient memory. Need $(LITE_MEM_REQUIRED) MB, have $${mem_mb} MB"; \
			echo "Tip: Increase Docker memory in Docker Desktop > Settings > Resources"; \
			exit 1; \
		fi; \
		echo "Memory OK"; \
	fi

check-mem-full: check
	@echo "Checking memory for full test (requires $(FULL_MEM_REQUIRED) MB)..."
	@mem=$$(docker info --format '{{.MemTotal}}' 2>/dev/null); \
	if [ -z "$$mem" ] || [ "$$mem" = "0" ]; then \
		echo "WARN: Could not detect Docker memory limit, skipping check"; \
	else \
		mem_mb=$$(echo "$$mem" | awk '{print int($$1/1024/1024)}'); \
		echo "Docker memory limit: $${mem_mb} MB"; \
		if [ "$${mem_mb}" -lt "$(FULL_MEM_REQUIRED)" ]; then \
			echo "ERROR: Insufficient memory. Need $(FULL_MEM_REQUIRED) MB, have $${mem_mb} MB"; \
			echo "Tip: Increase Docker memory in Docker Desktop > Settings > Resources"; \
			exit 1; \
		fi; \
		echo "Memory OK"; \
	fi

dirs:
	@mkdir -p $(TESTS_DIR)/mock_storage
	@mkdir -p $(TESTS_DIR)/mosquitto/config
	@mkdir -p $(TESTS_DIR)/frigate-lite

# Run tests with assertions (tears down after)
test: check
	@cd $(TESTS_DIR) && ./test-runner.sh

# Run tests but leave stack running for debugging
test-dev: check
	@cd $(TESTS_DIR) && ./test-runner.sh --no-teardown

# CI-friendly test run
test-ci: check
	@cd $(TESTS_DIR) && NO_COLOR=1 ./test-runner.sh

# Start stack without running tests
start: check check-mem-lite dirs
	@echo "Starting lightweight test stack..."
	@docker compose -f $(LITE_COMPOSE) up -d
	@echo "Waiting 20s for services..."
	@sleep 20
	@$(MAKE) -s status
	@echo ""
	@echo "Frigate: http://localhost:5000"

# Full test - 4 mock cameras with assertions
test-full: check
	@cd $(TESTS_DIR) && ./test-runner-full.sh

# Full test, leave stack running for debugging
test-full-dev: check
	@cd $(TESTS_DIR) && ./test-runner-full.sh --no-teardown

# Start full stack without tests
start-full: check check-mem-full dirs
	@echo "Starting mock camera harness..."
	@docker compose -f $(HARNESS_COMPOSE) up -d
	@echo "Waiting 15s for streams..."
	@sleep 15
	@echo "Starting security stack..."
	@docker compose -f $(FULL_COMPOSE) --env-file $(TESTS_DIR)/.env.mac up -d
	@echo "Waiting 20s for services..."
	@sleep 20
	@$(MAKE) -s status
	@echo ""
	@echo "Frigate: http://localhost:5000"

stop: check
	@docker compose -f $(LITE_COMPOSE) down 2>/dev/null || true
	@docker compose -f $(FULL_COMPOSE) down 2>/dev/null || true
	@docker compose -f $(HARNESS_COMPOSE) down 2>/dev/null || true
	@echo "Stopped"

status: check
	@echo "Containers:"
	@docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "frigate|mosquitto|mock|home" || echo "None running"

logs: check
	@docker compose -f $(LITE_COMPOSE) logs -f 2>/dev/null || \
		docker compose -f $(FULL_COMPOSE) logs -f 2>/dev/null || \
		echo "No stack running"

logs-frigate:
	@docker logs -f frigate-test 2>/dev/null || echo "Frigate not running"

clean: stop
	@rm -rf $(TESTS_DIR)/mock_storage
	@rm -rf $(TESTS_DIR)/frigate-lite/*.db
	@rm -rf $(TESTS_DIR)/mosquitto/data
	@rm -rf $(TESTS_DIR)/mosquitto/log
	@docker network rm edge-server-test 2>/dev/null || true
	@echo "Cleaned"

reset: clean
	@docker compose -f $(LITE_COMPOSE) pull 2>/dev/null || true
	@echo "Ready. Run: make test"

# Run shell script unit tests
test-scripts:
	@cd $(TESTS_DIR) && ./test-scripts.sh

lint:
	@command -v shellcheck >/dev/null || (echo "Install shellcheck: brew install shellcheck" && exit 1)
	@shellcheck -e SC1090,SC1091,SC2034 *.sh tests/*.sh
	@echo "Lint complete"

# Show memory usage of running test containers
mem: check
	@echo ""
	@echo "Container Memory Usage:"
	@echo "----------------------------------------"
	@docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" 2>/dev/null | grep -E "frigate|mosquitto|mock|stream|home" || echo "No test containers running"
	@echo ""
	@echo "Memory Limits (from compose):"
	@echo "----------------------------------------"
	@echo "Lite Stack (~1.3 GB total):"
	@echo "  mock-camera (mediamtx):    128 MB"
	@echo "  stream-gen (ffmpeg):       128 MB"
	@echo "  mosquitto:                  64 MB"
	@echo "  frigate:                 1,024 MB"
	@echo ""
	@echo "Full Stack (~3.2 GB total):"
	@echo "  rtsp-server (mediamtx):    ~50 MB"
	@echo "  mock-cam1..4 (ffmpeg x4): ~400 MB"
	@echo "  mosquitto:                 ~50 MB"
	@echo "  frigate:               ~1,500 MB"
	@echo "  homeassistant:           ~500 MB"
	@echo "  (+ Docker overhead):     ~700 MB"

# Test target container for deployment testing
TARGET_DIR := $(TESTS_DIR)/target
TARGET_COMPOSE := $(TARGET_DIR)/docker-compose.yml

target-start: check
	@cd $(TARGET_DIR) && ./setup.sh

target-stop: check
	@docker rm -f frigate homeassistant mosquitto 2>/dev/null || true
	@docker compose -f $(TARGET_COMPOSE) down 2>/dev/null || true
	@rm -rf /tmp/edge-server-test /tmp/edge-server-test-storage 2>/dev/null || true
	@echo "Test target and edge-server containers stopped"

target-ssh:
	@ssh -i $(TARGET_DIR)/ssh_keys/id_ed25519 -p 2222 testuser@localhost

# Victron Smart Shunt BLE tools
VICTRON_DIR := plugins/victron-shunt
VICTRON_SHUNT := $(VENV_DIR)/bin/victron-shunt

victron-setup:
	@if [ ! -f $(PIP) ]; then $(MAKE) setup; fi
	@echo "Installing victron-shunt CLI..."
	@$(PIP) install -e $(VICTRON_DIR)
	@echo "Done. Run: make victron-check"

victron-check: $(VICTRON_SHUNT)
	@$(VICTRON_SHUNT) check

victron-scan: $(VICTRON_SHUNT)
	@$(VICTRON_SHUNT) scan

victron-install:
	@echo "Installing victron-shunt to /opt/victron-shunt..."
	sudo mkdir -p /opt/victron-shunt
	sudo cp -r $(VICTRON_DIR)/src /opt/victron-shunt/
	sudo cp $(VICTRON_DIR)/pyproject.toml /opt/victron-shunt/
	sudo cp $(VICTRON_DIR)/requirements.txt /opt/victron-shunt/
	@echo "Creating virtual environment..."
	sudo python3 -m venv /opt/victron-shunt/venv
	sudo /opt/victron-shunt/venv/bin/pip install --upgrade pip -q
	sudo /opt/victron-shunt/venv/bin/pip install /opt/victron-shunt -q
	@echo "Installing systemd service..."
	sudo cp $(VICTRON_DIR)/victron-shunt.service /etc/systemd/system/
	sudo mkdir -p /etc/victron-shunt
	@if [ ! -f /etc/victron-shunt/config.yaml ]; then \
		echo "Creating default config (edit /etc/victron-shunt/config.yaml)..."; \
		echo 'address: "XX:XX:XX:XX:XX:XX"' | sudo tee /etc/victron-shunt/config.yaml > /dev/null; \
		echo 'key: "your-32-char-encryption-key"' | sudo tee -a /etc/victron-shunt/config.yaml > /dev/null; \
		echo 'mqtt:' | sudo tee -a /etc/victron-shunt/config.yaml > /dev/null; \
		echo '  host: localhost' | sudo tee -a /etc/victron-shunt/config.yaml > /dev/null; \
		echo '  port: 1883' | sudo tee -a /etc/victron-shunt/config.yaml > /dev/null; \
		echo '  user: admin' | sudo tee -a /etc/victron-shunt/config.yaml > /dev/null; \
		echo '  # password: from MQTT_PASS env var' | sudo tee -a /etc/victron-shunt/config.yaml > /dev/null; \
	fi
	sudo systemctl daemon-reload
	sudo systemctl enable victron-shunt
	@if grep -q "XX:XX:XX:XX:XX:XX" /etc/victron-shunt/config.yaml 2>/dev/null; then \
		echo "Done. Edit /etc/victron-shunt/config.yaml then: sudo systemctl start victron-shunt"; \
	else \
		echo "Starting victron-shunt service..."; \
		sudo systemctl start victron-shunt; \
		echo "Done. Check status: sudo systemctl status victron-shunt"; \
	fi

victron-test: $(VICTRON_SHUNT)
	@$(PIP) install pytest pytest-cov -q
	@cd $(VICTRON_DIR) && ../../$(VENV_DIR)/bin/python3 -m pytest tests/ -v

$(VICTRON_SHUNT):
	@$(MAKE) victron-setup

# NUT Power Monitor
NUT_DIR := plugins/nut-power

nut-install:
	@if [ ! -d /sys/class/power_supply ]; then \
		echo "ERROR: /sys/class/power_supply not found (not Linux?)"; \
		exit 1; \
	fi
	sudo $(NUT_DIR)/install.sh

nut-status:
	@upsc laptop 2>/dev/null || echo "NUT not installed or not running"

nut-uninstall:
	sudo $(NUT_DIR)/uninstall.sh
