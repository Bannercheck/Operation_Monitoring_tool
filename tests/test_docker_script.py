"""watchover.sh drives Docker Compose; here Docker is a fake on PATH that records the calls, so the script's own logic
(.env generation, secrets, image tag, command order, banner) is checked without a daemon."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$1 $2" in
  "compose version") echo "Docker Compose version v2.29" ;;
  "compose ps") [[ "$*" == *--format* ]] && { echo "dashboard healthy"; echo "ollama healthy"; } || echo "NAME  STATUS"; ;;
  "compose config") echo '{"volumes":{"watchover-data":{"name":"watchover_watchover-data"}}}' ;;
esac
exit 0
'''


@pytest.fixture
def stage(tmp_path):
    for f in ("watchover.sh", "docker-compose.yml", "docker-compose.edge.yml", ".env.docker.example", "pyproject.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    (tmp_path / "bin").mkdir(); fake = tmp_path / "bin" / "docker"; fake.write_text(FAKE); fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(tmp_path / "calls.log"))
    return tmp_path, env


def run(stage, *args):
    d, env = stage
    return subprocess.run(["bash", str(d / "watchover.sh"), *args], cwd=d, env=env, capture_output=True, text=True, timeout=60)


def test_install_writes_env_builds_and_starts(stage):
    d, _ = stage
    r = run(stage, "install")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    pw = [l for l in env.splitlines() if l.startswith("POSTGRES_PASSWORD=")][0].split("=", 1)[1]
    key = [l for l in env.splitlines() if l.startswith("MCP_API_KEY=")][0].split("=", 1)[1]
    assert len(pw) == 48 and pw != "change-me" and len(key) == 48 and "#MCP_API_KEY" not in env
    version = [l for l in (ROOT / "pyproject.toml").read_text().splitlines() if l.startswith("version")][0].split('"')[1]
    assert f"WATCHOVER_TAG={version}" in env and "WATCHOVER_IMAGE=watchover" in env
    assert oct((d / ".env").stat().st_mode)[-3:] == "600" and (d / "datasets").is_dir() and (d / "backups").is_dir()
    calls = (d / "calls.log").read_text()
    assert "compose build" in calls and f"--build-arg VERSION={version}" in calls and "compose up -d --remove-orphans" in calls
    assert calls.index("compose build") < calls.index("compose up")
    assert "8501" in r.stdout and "admin@watchover.local" in r.stdout and "Watchover v" in r.stdout
    # second run keeps the secrets, mcp on adds the profile
    run(stage, "update")
    assert f"POSTGRES_PASSWORD={pw}" in (d / ".env").read_text()
    r = run(stage, "mcp", "on")
    assert r.returncode == 0 and "WATCHOVER_MCP=1" in (d / ".env").read_text() and "--profile mcp up -d" in (d / "calls.log").read_text()


def test_usage_and_missing_docker(stage):
    d, env = stage
    r = run(stage, "bogus")
    assert r.returncode == 1 and "install" in r.stdout
    env["PATH"] = "/nonexistent"
    r = subprocess.run([shutil.which("bash"), str(d / "watchover.sh"), "install"], cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "Docker is not installed" in r.stderr


def test_edge_on_off(stage):
    """edge on <host>: Caddy profile, host in .env, plain UI port bound to localhost; edge off restores it."""
    d, _ = stage
    run(stage, "install")
    r = run(stage, "edge", "on", "watchover.sirket.local")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    assert "WATCHOVER_EDGE=1" in env and "WATCHOVER_EDGE_HOST=watchover.sirket.local" in env and "WATCHOVER_EDGE_TLS=internal" in env
    hosts = [l for l in env.splitlines() if l.startswith("WATCHOVER_EDGE_HOSTS=")][0]
    assert hosts.startswith("WATCHOVER_EDGE_HOSTS=watchover.sirket.local, ") and hosts.count(",") == 1          # the machine's IP is added
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.edge.yml" in env and "WATCHOVER_UI_PORT=127.0.0.1" not in env
    assert "https://watchover.sirket.local" in r.stdout and "--profile edge up -d --remove-orphans --force-recreate dashboard edge" in (d / "calls.log").read_text()
    assert (d / "certs").is_dir()
    r = run(stage, "edge", "off")
    env = (d / ".env").read_text()
    assert r.returncode == 0 and "WATCHOVER_EDGE=0" in env and "COMPOSE_FILE=" not in env and "WATCHOVER_UI_PORT=8501" in env
    r = run(stage, "edge")
    assert r.returncode == 1 and "usage" in r.stderr


def test_llm_on_off(stage):
    d, _ = stage
    run(stage, "install")
    r = run(stage, "llm", "on")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    assert "WATCHOVER_LLM=1" in env and "LLM_BASE_URL=http://ollama:11434" in env and "LLM_PROVIDER=ollama" in env
    assert "LLM_MODEL=qwen2.5:7b-instruct" in env and "LLM_EMBED_MODEL=bge-m3" in env
    calls = (d / "calls.log").read_text()
    assert "--profile llm" in calls and "ollama pull qwen2.5:7b-instruct" in calls and "ollama pull bge-m3" in calls
    r = run(stage, "llm", "off")
    assert r.returncode == 0 and "WATCHOVER_LLM=0" in (d / ".env").read_text() and "LLM_BASE_URL=" in (d / ".env").read_text()
    assert run(stage, "llm").returncode == 1


def test_llm_export_and_import(stage):
    d, _ = stage
    run(stage, "install"); run(stage, "llm", "on")
    r = run(stage, "llm", "export", "out.jsonl")
    assert r.returncode == 0 and "watchover.training export" in (d / "calls.log").read_text() and "cp watchover:/tmp/training.jsonl" in (d / "calls.log").read_text()
    m = d / "models" / "watchover-ops"; m.mkdir(parents=True); (m / "Modelfile").write_text("FROM qwen2.5:1.5b-instruct\n")
    r = run(stage, "llm", "import", str(m))
    calls = (d / "calls.log").read_text()
    assert r.returncode == 0, r.stderr
    assert "cp " in calls and "ollama create watchover-ops -f Modelfile" in calls and "LLM_MODEL=watchover-ops" in (d / ".env").read_text()
    assert run(stage, "llm", "import", str(d / "nope")).returncode == 1

