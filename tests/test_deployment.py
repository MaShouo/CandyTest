from __future__ import annotations

import os
import re
import io
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DockerDeploymentPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        cls.host = (ROOT / "compose.host.yaml").read_text(encoding="utf-8")
        cls.npm = (ROOT / "compose.npm.yaml").read_text(encoding="utf-8")
        cls.caddy = (ROOT / "compose.caddy.yaml").read_text(encoding="utf-8")
        cls.caddyfile = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        cls.deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        cls.env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        cls.dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        cls.gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        cls.start_bat = (ROOT / "start.bat").read_text(encoding="utf-8")
        cls.ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_windows_launcher_forces_local_loopback_mode(self):
        self.assertIn('set "CANDYTEST_DEPLOYMENT=local"', self.start_bat)
        self.assertIn('set "CANDYTEST_HOST=127.0.0.1"', self.start_bat)

    def test_dockerfile_uses_pinned_node_clis_without_build_toolchain(self):
        self.assertIn("FROM node:22-bookworm-slim", self.dockerfile)
        self.assertIn("ARG PI_VERSION=0.84.2", self.dockerfile)
        self.assertIn("ARG CODEX_VERSION=0.147.0", self.dockerfile)
        self.assertIn("@earendil-works/pi-coding-agent@${PI_VERSION}", self.dockerfile)
        self.assertIn("@openai/codex@${CODEX_VERSION}", self.dockerfile)
        self.assertNotRegex(self.dockerfile, r"(?i)\b(rust|cargo|build-essential)\b")
        for package in ("python3", "python3-venv", "python3-pip", "ca-certificates", "tini"):
            self.assertIn(package, self.dockerfile)

    def test_dockerfile_is_server_mode_read_only_compatible(self):
        self.assertIn("CANDYTEST_DEPLOYMENT=server", self.dockerfile)
        self.assertRegex(self.dockerfile, r"CANDYTEST_HOST=\S+")
        self.assertIn("CANDYTEST_PORT=8765", self.dockerfile)
        self.assertIn("CANDYTEST_DATA_DIR=/data", self.dockerfile)
        self.assertIn("HOME=/tmp/candytest-home", self.dockerfile)
        self.assertIn("XDG_CONFIG_HOME=/tmp/candytest-home/.config", self.dockerfile)
        self.assertIn("XDG_CACHE_HOME=/tmp/candytest-home/.cache", self.dockerfile)
        self.assertIn("XDG_DATA_HOME=/tmp/candytest-home/.local/share", self.dockerfile)
        self.assertIn("mkdir -p /app /data /tmp/candytest-home", self.dockerfile)
        self.assertIn("chown -R node:node", self.dockerfile)
        req_copy = self.dockerfile.index("COPY requirements.txt")
        pip_install = self.dockerfile.index(
            "/opt/venv/bin/pip install --no-cache-dir -r requirements.txt"
        )
        run_copy = self.dockerfile.index("COPY --chown=node:node run.py")
        app_copy = self.dockerfile.index("COPY --chown=node:node candytest")
        self.assertLess(req_copy, pip_install)
        self.assertLess(pip_install, run_copy)
        self.assertLess(run_copy, app_copy)
        self.assertLess(app_copy, self.dockerfile.index("USER node"))
        self.assertNotIn("chown -R node:node /app\n", self.dockerfile.replace("\r\n", "\n"))
        self.assertIn("USER node", self.dockerfile)
        self.assertIn("EXPOSE 8765", self.dockerfile)
        self.assertIn('VOLUME ["/data"]', self.dockerfile)
        self.assertIn('CMD ["sh", "-c", "mkdir -p', self.dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--"]', self.dockerfile)
        self.assertIn("/healthz", self.dockerfile)

    def test_base_compose_is_public_application_only(self):
        lowered = self.compose.lower()
        self.assertIn('image: "${CANDYTEST_IMAGE:-ghcr.io/mashouo/candytest:latest}"', self.compose)
        self.assertNotIn("build:", lowered)
        self.assertNotIn("ports:", lowered)
        self.assertNotIn("external:", lowered)
        self.assertNotIn("networks:", lowered)
        self.assertNotIn("caddy", lowered)
        self.assertNotIn("nginx", lowered)
        self.assertIn("CANDYTEST_DEPLOYMENT: server", self.compose)
        self.assertIn("CANDYTEST_HOST:", self.compose)
        self.assertIn("CANDYTEST_PORT: 8765", self.compose)
        self.assertIn("CANDYTEST_DATA_DIR: /data", self.compose)
        self.assertIn("candytest-data:/data", self.compose)
        self.assertIn("host.docker.internal:host-gateway", self.compose)
        for setting in (
            "read_only: true",
            "tmpfs:",
            "cap_drop:",
            "ALL",
            "no-new-privileges:true",
            "pids_limit: 512",
            "mem_limit:",
            "restart: unless-stopped",
            "healthcheck:",
        ):
            self.assertIn(setting, self.compose)

    def test_host_layer_only_adds_loopback_mapping(self):
        self.assertIn('127.0.0.1:${CANDYTEST_PORT:-8765}:8765', self.host)
        self.assertNotIn("CANDYTEST_BIND_ADDRESS", self.host)
        self.assertNotIn("build:", self.host)
        self.assertNotIn("external:", self.host)
        self.assertNotIn("caddy", self.host.lower())
        self.assertNotIn("npm", self.host.lower())

    def test_npm_layer_reuses_external_network_and_stable_alias(self):
        self.assertIn('name: "${NPM_NETWORK:?Set NPM_NETWORK', self.npm)
        self.assertIn("external: true", self.npm)
        self.assertIn("aliases:", self.npm)
        self.assertIn("- candytest", self.npm)
        self.assertNotIn("ports:", self.npm)
        self.assertNotIn("build:", self.npm)

    def test_caddy_layer_and_caddyfile_are_persistent_https_proxy(self):
        self.assertRegex(self.caddy, r"image:\s+caddy:[0-9.]+-alpine")
        self.assertIn('"80:80"', self.caddy)
        self.assertIn('"443:443"', self.caddy)
        self.assertIn("condition: service_healthy", self.caddy)
        self.assertIn("caddy-data:/data", self.caddy)
        self.assertIn("caddy-config:/config", self.caddy)
        self.assertIn("caddy:\n    internal: true", self.caddy)
        self.assertIn("caddy-egress", self.caddy)
        self.assertIn("${CANDYTEST_DOMAIN", self.caddy)
        self.assertIn("{$CANDYTEST_DOMAIN}", self.caddyfile)
        self.assertIn("reverse_proxy candytest:8765", self.caddyfile)

    def test_deploy_script_is_idempotent_v2_pull_only_and_operationally_safe(self):
        self.assertTrue(os.access(ROOT / "deploy.sh", os.X_OK))
        self.assertIn("set -Eeuo pipefail", self.deploy)
        self.assertIn("--project-name", self.deploy)
        self.assertIn('PROJECT_NAME="candytest"', self.deploy)
        self.assertIn('cd -- "$ROOT_DIR"', self.deploy)
        self.assertIn("docker compose", self.deploy)
        self.assertIn("compose pull", self.deploy)
        self.assertNotIn("compose pull candytest", self.deploy)
        self.assertIn("--no-build", self.deploy)
        self.assertNotIn("docker build", self.deploy.lower())
        self.assertNotRegex(self.deploy, r"docker compose[^\n]*--build(?:\s|$)")
        self.assertIn("mkdir -- \"$LOCK_DIR\"", self.deploy)
        self.assertIn("NetworkSettings.Networks", self.deploy)
        self.assertIn("docker network inspect", self.deploy)
        self.assertIn("external", self.npm)
        self.assertIn("wait_for_healthy", self.deploy)
        self.assertIn("unhealthy", self.deploy)
        self.assertIn("health_timeout", self.deploy)
        self.assertIn("backup_data_from_container", self.deploy)
        self.assertIn("docker run --rm --network none --volumes-from", self.deploy)
        self.assertIn("docker stop \"$container_id\"", self.deploy)
        self.assertIn("docker start \"$container_id\"", self.deploy)
        self.assertIn("restore_env_snapshot", self.deploy)
        self.assertIn("begin_env_transaction", self.deploy)
        self.assertIn("MODE_WAS_SET=0", self.deploy)
        self.assertIn("candytest-rescue:", self.deploy)
        self.assertIn("restore-staging", self.deploy)
        self.assertIn("restore-previous", self.deploy)
        self.assertIn("NPM mode requires a running Nginx Proxy Manager", self.deploy)
        self.assertIn("loopback-only host mode", self.deploy)
        self.assertNotIn("CANDYTEST_BIND_ADDRESS", self.deploy)
        self.assertIn('DATA_VOLUME="${PROJECT_NAME}_candytest-data"', self.deploy)
        self.assertIn("backup_data_from_named_volume", self.deploy)
        self.assertIn('bridge|host|none) fatal', self.deploy)
        self.assertNotIn("docker exec -i", self.deploy)
        self.assertNotIn("docker compose down -v", self.deploy)
        npm_detector = re.search(r"is_npm_container\(\) \{(?P<body>.*?)^\}", self.deploy, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(npm_detector)
        self.assertNotIn("[[:space:]_.-])npm", npm_detector.group("body"))

    def test_env_and_ignore_files_keep_state_and_secrets_private(self):
        self.assertIn("CANDYTEST_IMAGE=ghcr.io/mashouo/candytest:latest", self.env_example)
        self.assertIn("CANDYTEST_MODE=auto", self.env_example)
        self.assertNotIn("CANDYTEST_BIND_ADDRESS", self.env_example)
        self.assertIn("COOKIE_SECURE=1", self.env_example)
        self.assertIn("admin / admin", self.env_example)
        for entry in (".env", ".deploy", "deploy.sh", "compose*.yaml"):
            self.assertIn(entry, self.dockerignore)
        self.assertIn(".deploy/", self.gitignore)
        for marker in ("s" + "k-", "g" + "hp_", "github" + "_pat_", "xox" + "b-"):
            self.assertNotIn(marker, self.env_example)
        self.assertNotIn("scrypt:", self.env_example)
        self.assertNotIn("pbkdf2:", self.env_example)

    def test_ci_runs_smoke_with_overlay_and_publishes_private_multiplatform_ghcr(self):
        self.assertIn("ubuntu-latest", self.ci)
        self.assertIn("windows-latest", self.ci)
        self.assertIn("CANDYTEST_IMAGE=candytest:ci", self.ci)
        self.assertNotIn("CANDYTEST_BIND_ADDRESS", self.ci)
        self.assertIn("-f compose.yaml -f compose.host.yaml", self.ci)
        self.assertIn("-f compose.yaml -f compose.npm.yaml config --quiet", self.ci)
        self.assertIn("-f compose.yaml -f compose.caddy.yaml config --quiet", self.ci)
        self.assertIn("NPM_NETWORK=candytest-ci-npm", self.ci)
        self.assertIn("CANDYTEST_DOMAIN=candy.example.invalid", self.ci)
        self.assertIn("--no-build", self.ci)
        self.assertIn("publish:", self.ci)
        self.assertIn("packages: write", self.ci)
        self.assertIn("contents: read", self.ci)
        self.assertIn("ghcr.io/mashouo/candytest", self.ci)
        self.assertIn("linux/amd64,linux/arm64", self.ci)
        self.assertIn("docker/setup-qemu-action", self.ci)
        self.assertIn("docker/setup-buildx-action", self.ci)
        self.assertIn("docker/login-action", self.ci)
        self.assertIn("secrets.GITHUB_TOKEN", self.ci)
        self.assertIn("type=raw,value=latest", self.ci)
        self.assertIn("type=sha,prefix=sha-", self.ci)
        self.assertIn("type=ref,event=tag", self.ci)
        self.assertIn("needs: container", self.ci)
        self.assertIn('startsWith(github.ref, \'refs/tags/v\')', self.ci)
        self.assertNotIn("HASH_B64=", self.ci)
        for marker in ("password=admin", "/api/settings/account", "/api/runtime", "pi --version", "codex --version", "id -u"):
            self.assertIn(marker, self.ci)

    def test_readme_documents_one_command_modes_updates_and_npm_502(self):
        self.assertIn("chmod +x deploy.sh", self.readme)
        self.assertIn("./deploy.sh", self.readme)
        self.assertIn("./deploy.sh --mode caddy --domain", self.readme)
        self.assertIn("Scheme: http", self.readme)
        self.assertIn("Forward Hostname: candytest", self.readme)
        self.assertIn("Forward Port: 8765", self.readme)
        self.assertIn("candytest:8765", self.readme)
        self.assertIn("同一个 Docker 网络", self.readme)
        self.assertIn("固定监听 `127.0.0.1:8765`", self.readme)
        self.assertIn("公网访问必须经过", self.readme)
        self.assertNotIn("CANDYTEST_BIND_ADDRESS", self.readme)
        self.assertIn("./deploy.sh status", self.readme)
        self.assertIn("./deploy.sh logs", self.readme)
        self.assertIn("./deploy.sh restore", self.readme)
        self.assertIn("自动", self.readme)
        self.assertIn("down -v", self.readme)
        self.assertNotIn("docker compose up -d --build", self.readme)
        self.assertNotIn("docker compose up -d --no-build", self.readme)

    def test_compose_yaml_shapes_when_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")

        base = yaml.safe_load(self.compose)
        host = yaml.safe_load(self.host)
        npm = yaml.safe_load(self.npm)
        caddy = yaml.safe_load(self.caddy)
        self.assertEqual(set(base["services"]), {"candytest"})
        service = base["services"]["candytest"]
        self.assertEqual(service["image"], "${CANDYTEST_IMAGE:-ghcr.io/mashouo/candytest:latest}")
        self.assertEqual(set(service["volumes"]), {"candytest-data:/data"})
        self.assertNotIn("ports", service)
        self.assertNotIn("networks", base)
        self.assertEqual(host["services"]["candytest"]["ports"], ["127.0.0.1:${CANDYTEST_PORT:-8765}:8765"])
        self.assertTrue(npm["networks"]["npm"]["external"])
        self.assertEqual(set(npm["services"]["candytest"]["networks"]), {"npm"})
        self.assertIn("candytest", npm["services"]["candytest"]["networks"]["npm"]["aliases"])
        self.assertEqual(set(caddy["services"]), {"candytest", "caddy"})
        self.assertEqual(set(caddy["services"]["caddy"]["ports"]), {"80:80", "443:443"})


class DeploymentScriptShimTests(unittest.TestCase):
    """Exercise deployment decisions without a Docker daemon.

    The shim records Docker invocations and supplies only the inspect data each
    scenario needs. It validates the script's observable control flow, rather
    than treating a collection of function-name assertions as deployment tests.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = shutil.which("bash")

    def setUp(self) -> None:
        if not self.bash:
            self.skipTest("bash is required for deployment-script shim tests")
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        for name in (
            "deploy.sh",
            ".env.example",
            "compose.yaml",
            "compose.host.yaml",
            "compose.npm.yaml",
            "compose.caddy.yaml",
        ):
            shutil.copy2(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "deploy", self.root / "deploy")
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.log_path = self.root / "docker.log"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        fake_docker = self.fake_bin / "docker"
        fake_docker.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -eu
                printf '%q ' "$@" >> "$FAKE_DOCKER_LOG"
                printf '\\n' >> "$FAKE_DOCKER_LOG"

                case "$1" in
                  info)
                    exit 0
                    ;;
                  ps)
                    if [[ " $* " == *" -aq "* ]]; then
                      if [[ -n "${FAKE_CONTAINER:-}" ]]; then
                        printf '%s\\n' "$FAKE_CONTAINER"
                      fi
                    elif [[ -n "${FAKE_NPM_NAME:-}" ]]; then
                      printf '%s %s %s\\n' "npm-id" "${FAKE_NPM_IMAGE:-jc21/nginx-proxy-manager:latest}" "$FAKE_NPM_NAME"
                    fi
                    exit 0
                    ;;
                  inspect)
                    template="${3:-}"
                    if [[ "$template" == *"NetworkSettings.Networks"* ]]; then
                      printf '%s\\n' "${FAKE_NPM_NETWORKS:-}"
                    elif [[ "$template" == *"Config.Image"* ]]; then
                      printf '%s\\n' "${FAKE_IMAGE_REF:-ghcr.io/mashouo/candytest:previous}"
                    elif [[ "$template" == *".Image"* ]]; then
                      printf '%s\\n' "${FAKE_IMAGE_ID:-sha256:previous}"
                    elif [[ "$template" == *"State.Running"* ]]; then
                      printf '%s\\n' "${FAKE_RUNNING:-true}"
                    elif [[ "$template" == *"State.Health"* ]]; then
                      printf '%s\\n' "${FAKE_HEALTH:-healthy}"
                    elif [[ "$template" == *"State.Status"* ]]; then
                      printf '%s\\n' "running"
                    fi
                    exit 0
                    ;;
                  network)
                    exit 0
                    ;;
                  image)
                    exit 0
                    ;;
                  stop|start)
                    exit 0
                    ;;
                  run)
                    printf 'consistent-backup'
                    exit 0
                    ;;
                  compose)
                    arguments="$*"
                    if [[ "$arguments" == *" version"* ]]; then
                      exit 0
                    fi
                    if [[ "$arguments" == *" pull"* && "${FAKE_PULL_FAIL:-0}" == "1" ]]; then
                      exit 1
                    fi
                    if [[ "$arguments" == *" up -d"* && "${FAKE_FIRST_UP_FAIL:-0}" == "1" ]]; then
                      counter="$FAKE_STATE_DIR/up-count"
                      count=0
                      [[ -f "$counter" ]] && count=$(cat "$counter")
                      count=$((count + 1))
                      printf '%s' "$count" > "$counter"
                      if [[ "$count" -eq 1 ]]; then
                        exit 1
                      fi
                    fi
                    exit 0
                    ;;
                  *)
                    exit 0
                    ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def write_env(self, text: str) -> bytes:
        path = self.root / ".env"
        path.write_text(text, encoding="utf-8", newline="")
        return path.read_bytes()

    def run_deploy(self, *arguments: str, **fake_env: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(fake_env)
        environment["FAKE_DOCKER_LOG"] = str(self.log_path)
        environment["FAKE_STATE_DIR"] = str(self.state_dir)
        environment["PATH"] = str(self.fake_bin) + os.pathsep + environment.get("PATH", "")
        return subprocess.run(
            [self.bash, "deploy.sh", *arguments],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def docker_log(self) -> str:
        return self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""

    def test_backup_uses_named_volume_when_container_was_removed(self):
        destination = self.root / "volume-backup.tar.gz"
        self.write_env(
            "CANDYTEST_IMAGE=ghcr.io/mashouo/candytest:previous\n"
            "CANDYTEST_MODE=host\n"
            "CANDYTEST_HEALTH_TIMEOUT=1\n"
        )

        result = self.run_deploy("backup", destination.name)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(destination.exists())
        log = self.docker_log()
        self.assertIn("volume inspect candytest_candytest-data", log)
        self.assertIn("-v candytest_candytest-data:/data:ro", log)

    def test_default_update_retains_existing_caddy_mode(self):
        self.write_env(
            "CANDYTEST_IMAGE=ghcr.io/mashouo/candytest:previous\n"
            "CANDYTEST_MODE=caddy\n"
            "CANDYTEST_DOMAIN=candy.example.com\n"
            "CANDYTEST_HEALTH_TIMEOUT=1\n"
        )

        result = self.run_deploy(FAKE_CONTAINER="old-caddy", FAKE_RUNNING="false")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CANDYTEST_MODE=caddy", (self.root / ".env").read_text(encoding="utf-8"))
        compose_commands = [line for line in self.docker_log().splitlines() if "compose" in line]
        self.assertTrue(any("compose.caddy.yaml" in line and "up" in line for line in compose_commands))
        self.assertFalse(any("compose.host.yaml" in line for line in compose_commands))

    def test_pull_failure_restores_exact_original_env(self):
        original = self.write_env(
            "# Preserve this complete file exactly.\n"
            "CANDYTEST_IMAGE=ghcr.io/mashouo/candytest:previous\n"
            "CANDYTEST_MODE=caddy\n"
            "CANDYTEST_DOMAIN=candy.example.com\n"
            "CANDYTEST_HEALTH_TIMEOUT=1\n"
        )

        result = self.run_deploy("--mode", "host", FAKE_PULL_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertIn("pull", self.docker_log())

    def test_update_pulls_all_services_before_backup_stop(self):
        self.write_env(
            "CANDYTEST_IMAGE=ghcr.io/mashouo/candytest:previous\n"
            "CANDYTEST_MODE=caddy\n"
            "CANDYTEST_DOMAIN=candy.example.com\n"
            "CANDYTEST_HEALTH_TIMEOUT=1\n"
        )

        result = self.run_deploy(FAKE_CONTAINER="old-caddy", FAKE_RUNNING="true")

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.docker_log()
        pull_lines = [
            line
            for line in log.splitlines()
            if "compose" in line and re.search(r"(?:^| )pull(?: |$)", line) and "version" not in line
        ]
        self.assertEqual(len(pull_lines), 1, log)
        pull_line = pull_lines[0]
        self.assertIn("compose.caddy.yaml", pull_line)
        self.assertNotIn("pull candytest", pull_line)
        config_index = log.index("config --quiet")
        pull_index = log.index(pull_line)
        stop_index = log.index("stop old-caddy")
        backup_index = log.index("--volumes-from old-caddy:ro")
        up_index = log.index("up -d")
        self.assertLess(config_index, pull_index)
        self.assertLess(pull_index, stop_index)
        self.assertLess(stop_index, backup_index)
        self.assertLess(backup_index, up_index)

    def test_pull_failure_does_not_stop_or_backup_existing_service(self):
        original = self.write_env(
            "CANDYTEST_IMAGE=ghcr.io/mashouo/candytest:previous\n"
            "CANDYTEST_MODE=host\n"
            "CANDYTEST_HEALTH_TIMEOUT=1\n"
        )

        result = self.run_deploy(FAKE_CONTAINER="old-running", FAKE_PULL_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / ".env").read_bytes(), original)
        log = self.docker_log()
        self.assertIn(" pull", log)
        self.assertNotIn("stop old-running", log)
        self.assertNotIn("volumes-from", log)
        self.assertNotIn("up -d", log)

    def test_failed_update_rolls_back_with_rescue_tag_without_changing_env_image(self):
        original = self.write_env(
            "CANDYTEST_IMAGE=ghcr.io/mashouo/candytest:previous\n"
            "CANDYTEST_MODE=caddy\n"
            "CANDYTEST_DOMAIN=candy.example.com\n"
            "CANDYTEST_HEALTH_TIMEOUT=1\n"
        )

        result = self.run_deploy(FAKE_CONTAINER="old-caddy", FAKE_FIRST_UP_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertNotIn("sha256:previous", (self.root / ".env").read_text(encoding="utf-8"))
        log = self.docker_log()
        self.assertIn("image tag sha256:previous candytest-rescue:", log)
        self.assertIn("rescue-env", log)
        self.assertIn("compose.caddy.yaml", log)

    def test_backup_stops_helpers_and_restarts_running_container(self):
        destination = self.root / "backup.tar.gz"
        self.write_env("CANDYTEST_MODE=host\nCANDYTEST_HEALTH_TIMEOUT=1\n")

        result = self.run_deploy("backup", destination.name, FAKE_CONTAINER="old-running")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(destination.exists())
        log = self.docker_log()
        stop_index = log.index("stop old-running")
        helper_index = log.index("run --rm --network none --volumes-from old-running:ro")
        start_index = log.index("start old-running")
        self.assertLess(stop_index, helper_index)
        self.assertLess(helper_index, start_index)

    def test_restore_uses_staging_switch_instead_of_deleting_live_data_first(self):
        archive = self.root / "restore.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            payload = b"restored"
            entry = tarfile.TarInfo("candytest.sqlite3")
            entry.size = len(payload)
            tar.addfile(entry, io.BytesIO(payload))
        self.write_env("CANDYTEST_MODE=host\nCANDYTEST_HEALTH_TIMEOUT=1\n")

        result = self.run_deploy("restore", archive.name, FAKE_CONTAINER="old-stopped", FAKE_RUNNING="false")

        self.assertEqual(result.returncode, 0, result.stderr)
        run_commands = [line for line in self.docker_log().splitlines() if " run --rm --no-deps -T candytest " in line]
        self.assertEqual(len(run_commands), 1)
        restore_command = run_commands[0]
        self.assertIn("restore-staging", restore_command)
        self.assertIn("restore-previous", restore_command)
        self.assertIn("move_live_data", restore_command)
        self.assertNotIn("find\\ /data\\ -mindepth\\ 1\\ -maxdepth\\ 1\\ -exec\\ rm", restore_command)

    def test_explicit_npm_network_must_be_attached_to_real_npm(self):
        result = self.run_deploy(
            "--mode",
            "npm",
            "--npm-network",
            "npm_default",
            FAKE_NPM_NAME="nginx-proxy-manager",
            FAKE_NPM_NETWORKS="another_network",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not attached", result.stderr)
        self.assertNotIn(" pull", self.docker_log())
        self.assertFalse((self.root / ".env").exists())

    def test_builtin_bridge_is_rejected_for_npm(self):
        result = self.run_deploy(
            "--mode",
            "npm",
            "--npm-network",
            "bridge",
            FAKE_NPM_NAME="nginx-proxy-manager",
            FAKE_NPM_NETWORKS="bridge",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user-defined Docker network", result.stderr)
        self.assertNotIn(" pull", self.docker_log())

    def test_arbitrary_npm_named_container_is_not_treated_as_npm(self):
        result = self.run_deploy(
            "--mode",
            "npm",
            "--npm-network",
            "npm_default",
            FAKE_NPM_NAME="npm",
            FAKE_NPM_IMAGE="example/reverse-proxy:latest",
            FAKE_NPM_NETWORKS="npm_default",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a running Nginx Proxy Manager", result.stderr)
        self.assertNotIn(" pull", self.docker_log())
