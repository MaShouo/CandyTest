from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DockerDeploymentPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
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
        self.assertIn("CANDYTEST_HOST=0.0.0.0", self.dockerfile)
        self.assertIn("CANDYTEST_PORT=8765", self.dockerfile)
        self.assertIn("CANDYTEST_DATA_DIR=/data", self.dockerfile)
        self.assertIn("HOME=/tmp/candytest-home", self.dockerfile)
        self.assertIn("XDG_CONFIG_HOME=/tmp/candytest-home/.config", self.dockerfile)
        self.assertIn("XDG_CACHE_HOME=/tmp/candytest-home/.cache", self.dockerfile)
        self.assertIn("XDG_DATA_HOME=/tmp/candytest-home/.local/share", self.dockerfile)
        self.assertIn("mkdir -p /app /data /tmp/candytest-home", self.dockerfile)
        self.assertIn("chown -R node:node", self.dockerfile)
        self.assertIn("USER node", self.dockerfile)
        self.assertIn("EXPOSE 8765", self.dockerfile)
        self.assertIn('VOLUME ["/data"]', self.dockerfile)
        self.assertIn('CMD ["sh", "-c", "mkdir -p', self.dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--"]', self.dockerfile)
        self.assertIn("/healthz", self.dockerfile)

    def test_compose_has_one_local_service_and_no_reverse_proxy(self):
        lowered = self.compose.lower()
        self.assertNotIn("caddy", lowered)
        self.assertNotIn("nginx", lowered)
        self.assertNotIn("reverse_proxy", lowered)
        self.assertNotIn("deploy:", lowered)
        self.assertIn("image: candytest:local", self.compose)
        self.assertIn("build:", self.compose)
        self.assertNotIn("ghcr.io", lowered)
        self.assertNotIn("CANDYTEST_BIND_ADDRESS", self.compose)
        loopback = ".".join(("127", "0", "0", "1"))
        self.assertIn(f'- "{loopback}:${{CANDYTEST_PORT:-8765}}:8765"', self.compose)
        self.assertIn("CANDYTEST_HOST: 0.0.0.0", self.compose)
        self.assertIn("CANDYTEST_DATA_DIR: /data", self.compose)
        self.assertIn("CANDYTEST_ADMIN_USERNAME", self.compose)
        self.assertIn('"${ADMIN_USERNAME:-}"', self.compose)
        self.assertIn("CANDYTEST_ADMIN_PASSWORD_HASH_B64", self.compose)
        self.assertIn('"${PASSWORD_HASH_B64:-}"', self.compose)
        self.assertIn("CANDYTEST_SECRET_KEY", self.compose)
        self.assertIn('"${SECRET_KEY:-}"', self.compose)
        self.assertNotIn(":?Set ", self.compose)
        self.assertIn("CANDYTEST_COOKIE_SECURE", self.compose)
        self.assertIn("candytest-data:/data", self.compose)
        self.assertIn("host.docker.internal:host-gateway", self.compose)
        for setting in ("read_only: true", "tmpfs:", "cap_drop:", "ALL", "no-new-privileges:true", "pids_limit: 512", "mem_limit:", "restart: unless-stopped", "healthcheck:"):
            self.assertIn(setting, self.compose)

    def test_env_example_documents_default_login_and_optional_migration_inputs(self):
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in self.env_example.splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        self.assertEqual(values["ADMIN_USERNAME"], "")
        self.assertEqual(values["PASSWORD_HASH_B64"], "")
        self.assertEqual(values["SECRET_KEY"], "")
        self.assertEqual(values["COOKIE_SECURE"], "1")
        self.assertIn("admin / admin", self.env_example)
        self.assertIn("optional", self.env_example.lower())
        for marker in ("s" + "k-", "g" + "hp_", "github" + "_pat_", "xox" + "b-"):
            self.assertNotIn(marker, self.env_example)
        self.assertNotIn("scrypt:", self.env_example)
        self.assertNotIn("pbkdf2:", self.env_example)

    def test_dockerignore_excludes_local_secrets_and_test_files(self):
        for entry in (".git", ".venv", "tests", "*.py[cod]", "*.sqlite3", "webdav.json", "auth.json", "auth.json.tmp-*", ".env", "*.pem", "*.key", "*.crt"):
            self.assertIn(entry, self.dockerignore)
        for entry in ("auth.json", "auth.json.tmp-*"):
            self.assertIn(entry, self.gitignore)
        self.assertIn("!.env.example", self.dockerignore)

    def test_ci_exercises_compose_login_and_container_clis(self):
        self.assertIn("docker compose --project-name candytest-ci up", self.ci)
        self.assertNotIn("HASH_B64=", self.ci)
        self.assertIn("password=admin", self.ci)
        self.assertIn("/api/settings/account", self.ci)
        self.assertIn("/api/runtime", self.ci)
        self.assertIn("pi --version", self.ci)
        self.assertIn("codex --version", self.ci)
        self.assertIn("id -u", self.ci)

    def test_readme_is_manual_sync_only_and_has_no_bundled_proxy_config(self):
        self.assertIn("没有启动自动 Pull，也没有退出自动 Push", self.readme)
        self.assertNotIn("启动时自动 Pull", self.readme)
        self.assertNotIn("正常退出时自动 Push", self.readme)
        self.assertIn("不包含 Nginx、Caddy、证书或反向代理配置", self.readme)

    def test_no_proxy_configuration_files_are_added(self):
        forbidden = []
        for relative in ("deploy/Caddyfile", "nginx.conf", "deploy/nginx.conf", "Caddyfile"):
            if (ROOT / relative).exists():
                forbidden.append(relative)
        self.assertEqual(forbidden, [])

    def test_compose_yaml_shape_when_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        document = yaml.safe_load(self.compose)
        self.assertEqual(set(document["services"]), {"candytest"})
        service = document["services"]["candytest"]
        self.assertEqual(set(service["volumes"]), {"candytest-data:/data"})
        self.assertNotIn("deploy", service)
        self.assertEqual(service["environment"]["CANDYTEST_DEPLOYMENT"], "server")


if __name__ == "__main__":
    unittest.main()
