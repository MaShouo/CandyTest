from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentFilesTests(unittest.TestCase):
    def text(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_docker_image_is_non_root_and_has_no_rust_toolchain(self):
        dockerfile = self.text("Dockerfile")
        lowered = dockerfile.lower()
        self.assertIn("from node:22-bookworm-slim", lowered)
        self.assertIn("user node", lowered)
        self.assertIn("candytest_deployment=server", lowered)
        self.assertIn("candytest_data_dir=/data", lowered)
        self.assertIn("home=/tmp/candytest-home", lowered)
        self.assertIn("@earendil-works/pi-coding-agent@${pi_version}", lowered)
        self.assertIn("@openai/codex@${codex_version}", lowered)
        self.assertIn("/healthz", lowered)
        for forbidden in ("cargo install", "rustup", "build-essential"):
            self.assertNotIn(forbidden, lowered.replace(
                "# runtime-only packages: do not add rust, a compiler toolchain, or build-essential.", ""
            ))

    def test_compose_does_not_publish_the_application_port(self):
        compose = self.text("compose.yaml")
        candytest_section = compose.split("\n  caddy:\n", 1)[0]
        self.assertIn("image: ghcr.io/${GITHUB_OWNER", candytest_section)
        self.assertIn("expose:\n      - \"8765\"", candytest_section)
        self.assertNotRegex(candytest_section, r"(?m)^    ports:")
        self.assertIn("read_only: true", candytest_section)
        self.assertIn("no-new-privileges:true", candytest_section)
        self.assertIn("candytest-data:/data", candytest_section)
        self.assertIn("HOME: /tmp/candytest-home", candytest_section)
        self.assertIn('"80:80"', compose)
        self.assertIn('"443:443"', compose)

    def test_caddy_authenticates_every_route_before_proxying(self):
        caddyfile = self.text("deploy/Caddyfile")
        auth_index = caddyfile.index("basic_auth")
        proxy_index = caddyfile.index("reverse_proxy")
        self.assertLess(auth_index, proxy_index)
        self.assertIn("{$CANDYTEST_DOMAIN}", caddyfile)
        self.assertIn("{$CADDY_BASIC_AUTH_HASH}", caddyfile)
        self.assertIn("header_up -Authorization", caddyfile)
        self.assertIn("Strict-Transport-Security", caddyfile)
        self.assertNotIn("REPLACE_WITH", caddyfile)

    def test_release_contains_local_runtime_only_and_publishes_ghcr(self):
        workflow = self.text(".github/workflows/release.yml")
        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("Copy-Item candytest", workflow)
        self.assertIn("Copy-Item run.py, start.bat, requirements.txt", workflow)
        self.assertIn("Copy-Item README-WINDOWS.md", workflow)
        package_block = workflow.split("- name: Build local-only ZIP", 1)[1].split(
            "- name: Upload local package", 1
        )[0]
        for server_file in ("Dockerfile", "compose.yaml", "Caddyfile", ".env.example"):
            self.assertNotIn(f"Copy-Item {server_file}", package_block)
        self.assertIn("docker/build-push-action@v6", workflow)
        self.assertIn("ghcr.io/${GITHUB_REPOSITORY,,}", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("contents: write", workflow)
        self.assertNotIn("--verify-tag", workflow)
        self.assertIn("flavor: latest=auto", workflow)

        ci = self.text(".github/workflows/ci.yml")
        self.assertIn("docker compose config --quiet", ci)
        self.assertIn("caddy validate --config", ci)
        self.assertIn("docker/build-push-action@v6", ci)
        self.assertIn("docker exec candytest-ci pi --version", ci)
        self.assertIn("docker exec candytest-ci codex --version", ci)

    def test_local_launcher_forces_local_mode(self):
        launcher = self.text("start.bat")
        self.assertIn('set "CANDYTEST_DEPLOYMENT=local"', launcher)
        self.assertIn('set "CANDYTEST_HOST=127.0.0.1"', launcher)
        self.assertNotIn('set "CANDYTEST_HOST=0.0.0.0"', launcher)

    def test_example_environment_contains_only_placeholders(self):
        example = self.text(".env.example")
        self.assertIn("REPLACE_WITH_PUBLIC_DOMAIN", example)
        self.assertIn("REPLACE_WITH_ACME_EMAIL", example)
        self.assertIn("REPLACE_WITH_BASIC_AUTH_USERNAME", example)
        self.assertIn("REPLACE_WITH_GENERATED_BCRYPT_HASH", example)
        self.assertNotRegex(example, re.compile(r"(?i)(password|token|secret)\s*=\s*[^#\r\n]+"))
        self.assertIn("CADDY_BASIC_AUTH_HASH='", example)


if __name__ == "__main__":
    unittest.main(verbosity=2)
