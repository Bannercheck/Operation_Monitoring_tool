"""The OpenShift deployment (deploy/openshift): every workload fits the restricted-v2 SCC (non-root, no privilege escalation, no
capabilities, RuntimeDefault seccomp, no fixed UID, ports >= 1024), has resources and probes, and no secret value is written in the
chart, the rendered manifest or the guide. The committed oc-apply manifest is always checked; with helm on PATH (or WATCHOVER_HELM)
the chart is rendered in several shapes too and the committed manifest must match a fresh render."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "openshift" / "watchover"
RENDERED = ROOT / "deploy" / "openshift" / "rendered" / "watchover.yaml"
HELM = os.environ.get("WATCHOVER_HELM") or shutil.which("helm")
RENDER_ARGS = ["--set", "postgresql.existingSecret=watchover-db", "--set", "routes.ui.host=watchover.apps.example.com",
               "--set", "routes.ingest.host=watchover-ingest.apps.example.com"]
SHAPES = {
    "default": [],
    "everything": ["--set", "llm.enabled=true,trainer.enabled=true,backup.enabled=true,llm.ollama.allowEgress=false,mcp.enabled=true",
                   "--set", "routes.ui.host=w.apps.example.com,routes.ingest.host=wi.apps.example.com"],
    "official-postgres": ["--set", "postgresql.flavor=official,postgresql.image=docker.io/library/postgres:16,backup.enabled=true"],
    "external-db": ["--set", "postgresql.enabled=false,externalDatabase.existingSecret=extdb,llm.enabled=true,llm.lock=false"],
}
INGEST_PATHS = {"/ingest", "/enroll", "/agent.py", "/agent/install.sh", "/agent/manifest.json", "/agent/boot.sh", "/agent/ca.crt", "/agent/update",
                "/agent/wo-agent-", "/agent/v1/", "/mobile", "/rum"}


def render(*args) -> list:
    out = subprocess.run([HELM, "template", "watchover", str(CHART), "-n", "watchover", *args], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def committed() -> list:
    return [d for d in yaml.safe_load_all(RENDERED.read_text(encoding="utf-8")) if d]


def manifests():
    yield "rendered/watchover.yaml", committed()
    if HELM:
        for name, args in SHAPES.items():
            yield name, render(*args)


def pod_specs(docs):
    for d in docs:
        k = d["kind"]
        if k in ("Deployment", "StatefulSet"):
            yield d, d["spec"]["template"]["spec"], True
        elif k == "CronJob":
            yield d, d["spec"]["jobTemplate"]["spec"]["template"]["spec"], False


def test_committed_manifest_parses_and_has_the_whole_installation():
    docs = committed()
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    for k in [("Deployment", "watchover"), ("StatefulSet", "watchover-postgresql"), ("Service", "watchover"), ("Service", "watchover-postgresql"),
              ("Route", "watchover"), ("PersistentVolumeClaim", "watchover-data"), ("PersistentVolumeClaim", "watchover-logs-hot"),
              ("PersistentVolumeClaim", "watchover-logs-cold"), ("NetworkPolicy", "watchover-postgresql"), ("ServiceAccount", "watchover")]:
        assert k in kinds, k
    assert not [d for d in docs if d["kind"] == "Secret"]                   # the oc-apply path creates the Secret by hand (guide)
    assert all(d["metadata"].get("namespace") == "watchover" for d in docs)


@pytest.mark.parametrize("shape", ["rendered"] + (list(SHAPES) if HELM else []))
def test_restricted_scc_resources_and_probes(shape):
    docs = committed() if shape == "rendered" else render(*SHAPES[shape])
    seen = 0
    for d, spec, long_running in pod_specs(docs):
        name = f"{d['kind']}/{d['metadata']['name']}"
        psc = spec.get("securityContext", {})
        assert psc.get("runAsNonRoot") is True and psc.get("seccompProfile", {}).get("type") == "RuntimeDefault", name
        assert "runAsUser" not in psc and "fsGroup" not in psc, f"{name}: the SCC assigns UID / GID"
        assert spec.get("automountServiceAccountToken") is False and not spec.get("hostNetwork") and not spec.get("hostPID"), name
        for v in spec.get("volumes", []):
            assert "hostPath" not in v, name
        for c in spec["containers"] + spec.get("initContainers", []):
            cn = f"{name}/{c['name']}"
            sc = c.get("securityContext", {})
            assert sc.get("allowPrivilegeEscalation") is False and sc.get("runAsNonRoot") is True, cn
            assert sc.get("capabilities", {}).get("drop") == ["ALL"] and not sc.get("capabilities", {}).get("add"), cn
            assert sc.get("seccompProfile", {}).get("type") == "RuntimeDefault" and not sc.get("privileged"), cn
            assert sc.get("readOnlyRootFilesystem") is True, cn
            assert "runAsUser" not in sc, cn
            res = c.get("resources", {})
            for kind in ("requests", "limits"):
                assert res.get(kind, {}).get("cpu") and res.get(kind, {}).get("memory"), f"{cn}: {kind}"
            assert not any("gpu" in k.lower() for k in res.get("limits", {})), f"{cn}: no GPU"
            for p in c.get("ports", []):
                assert p["containerPort"] >= 1024, cn
            if long_running:
                assert c.get("livenessProbe") and c.get("readinessProbe"), f"{cn}: probes"
            seen += 1
    assert seen >= 3


@pytest.mark.parametrize("shape", ["rendered"] + (["everything"] if HELM else []))
def test_one_pod_shares_data_logs_on_their_own_volumes_production_mode(shape):
    docs = committed() if shape == "rendered" else render(*SHAPES[shape])
    app = next(d for d in docs if d["kind"] == "Deployment" and d["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "app")
    spec = app["spec"]["template"]["spec"]
    by = {c["name"]: c for c in spec["containers"]}
    dash, recv = by["dashboard"], by["receiver"]
    env = {e["name"]: e for e in dash["env"]}
    mounts = {m["mountPath"]: m["name"] for m in dash["volumeMounts"]}
    claims = {v["name"]: v.get("persistentVolumeClaim", {}).get("claimName") for v in spec["volumes"]}
    assert "WATCHOVER_DEMO" not in env                     # the simulator is retired: no switch
    assert claims[mounts[env["WATCHOVER_LOG_HOT_DIR"]["value"]]].endswith("-logs-hot")
    assert claims[mounts[env["WATCHOVER_LOG_COLD_DIR"]["value"]]].endswith("-logs-cold")
    assert mounts["/data"] == "data" and {m["mountPath"]: m["name"] for m in recv["volumeMounts"]}["/data"] == "data"
    assert "secretKeyRef" in env["POSTGRES_PASSWORD"]["valueFrom"] and "$(POSTGRES_PASSWORD)" in env["DATABASE_URL"]["value"]
    assert [p["containerPort"] for p in dash["ports"]] == [8501] and [p["containerPort"] for p in recv["ports"]] == [8600, 4317, 4318]   # agents + OTLP gRPC / HTTP
    assert dash["livenessProbe"]["httpGet"]["path"] == "/api/health" and recv["livenessProbe"]["httpGet"]["path"] == "/health"
    assert app["spec"]["strategy"]["type"] == "Recreate" and spec["enableServiceLinks"] is False
    pvcs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "PersistentVolumeClaim"}
    assert pvcs[claims["logs-hot"]]["spec"]["resources"]["requests"]["storage"] == "50Gi"
    assert pvcs[claims["logs-cold"]]["spec"]["resources"]["requests"]["storage"] == "100Gi"          # a modest default; sized per install
    settings = __import__("json").loads(env["WATCHOVER_SETTINGS"]["value"])
    assert settings["live_retention_h"] == 48 and {"cold_retention_days", "hot_max_gb", "cold_max_gb"} <= set(settings)
    assert settings["cold_retention_days"] == 365 and settings["cold_max_gb"] == 0                   # a year for audits; no cap deletes


@pytest.mark.parametrize("shape", ["rendered"] + (["everything"] if HELM else []))
def test_routes_edge_tls_and_ingest_paths(shape):
    docs = committed() if shape == "rendered" else render(*SHAPES[shape])
    routes = [d for d in docs if d["kind"] == "Route"]
    assert routes and all(r["spec"]["tls"]["termination"] == "edge" and r["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect" for r in routes)
    ingest = [r for r in routes if r["spec"]["port"]["targetPort"] == "ingest"]
    assert INGEST_PATHS <= {r["spec"].get("path") for r in ingest}
    ui = [r for r in routes if r["spec"]["port"]["targetPort"] == "http"]
    assert len(ui) == 1 and not ui[0]["spec"].get("path")
    # the router replaces X-Forwarded-For: with WATCHOVER_FORWARDED_ALLOW="*" a client's own value would beat the sign-in throttle
    assert ui[0]["metadata"]["annotations"]["haproxy.router.openshift.io/set-forwarded-headers"] == "replace"


@pytest.mark.skipif(not HELM, reason="helm not installed")
def test_network_policies_and_llm_shape():
    docs = render(*SHAPES["everything"])
    np_ = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    pg = np_["watchover-postgresql"]["spec"]
    sources = [f["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for f in pg["ingress"][0]["from"]]
    assert sorted(sources) == ["app", "backup", "mcp"] and pg["ingress"][0]["ports"] == [{"port": 5432, "protocol": "TCP"}]
    ol = np_["watchover-ollama"]["spec"]
    assert [f["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for f in ol["ingress"][0]["from"]] == ["app"]
    assert ol["egress"] == [] and "Egress" in ol["policyTypes"]                    # allowEgress=false: internal model only
    app = np_["watchover-app"]["spec"]["ingress"][0]
    assert app["from"][0]["namespaceSelector"]["matchLabels"] == {"network.openshift.io/policy-group": "ingress"}
    ollama = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "watchover-ollama")
    c = ollama["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"]: e.get("value") for e in c["env"]}["WO_MODELS"] == "qwen2.5:3b-instruct"
    assert not any("gpu" in k for k in c["resources"]["limits"])
    dash = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "watchover")["spec"]["template"]["spec"]["containers"][0]
    import json
    pinned = json.loads({e["name"]: e.get("value") for e in dash["env"]}["WATCHOVER_SETTINGS"])
    assert pinned["llm_base"] == "http://watchover-ollama:11434" and pinned["llm_provider"] == "ollama" and pinned["llm_key"] == ""
    off = render()                                                                  # default: LLM off and locked
    dash = next(d for d in off if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
    pinned = json.loads({e["name"]: e.get("value") for e in dash["env"]}["WATCHOVER_SETTINGS"])
    assert pinned["llm_base"] == "" and pinned["llm_model"] == "" and not [d for d in off if "ollama" in d["metadata"]["name"]]


@pytest.mark.skipif(not HELM, reason="helm not installed")
def test_generated_password_secret_only_when_no_existing_secret():
    docs = render()
    sec = [d for d in docs if d["kind"] == "Secret"]
    assert len(sec) == 1 and set(sec[0]["data"]) == {"postgres-password"} and sec[0]["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert [d for d in render() if d["kind"] == "Secret"][0]["data"] != sec[0]["data"]                       # random per fresh install
    assert not [d for d in render("--set", "postgresql.existingSecret=mine") if d["kind"] == "Secret"]


@pytest.mark.skipif(not HELM, reason="helm not installed")
def test_mcp_server_off_by_default_its_own_pod_route_and_kept_key_when_on():
    """mcp.enabled: the MCP server as its own Deployment (a crash never touches the dashboard pod), same image, tini kept as PID 1,
    the key only from a Secret (generated, kept across upgrades, or yours), edge-TLS Route, NetworkPolicy, PostgreSQL reachable."""
    import base64
    assert not [d for d in render() + committed() if "mcp" in d["metadata"]["name"]]                    # off by default
    docs = render("--set", "mcp.enabled=true,mcp.route.host=watchover-mcp.apps.example.com,mcp.clientNamespaces[0].name=agents")
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    dep = by[("Deployment", "watchover-mcp")]
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert dep["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "mcp" and dep["spec"]["strategy"]["type"] == "Recreate"
    assert c["args"] == ["python", "mcp_server.py", "--http", "--port", "8765"] and "command" not in c
    assert c["image"].endswith("/watchover:" + yaml.safe_load((CHART / "Chart.yaml").read_text())["appVersion"])
    assert [p["containerPort"] for p in c["ports"]] == [8765]
    env = {e["name"]: e for e in c["env"]}
    assert env["MCP_API_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "watchover-mcp", "key": "mcp-api-key"} and "value" not in env["MCP_API_KEY"]
    assert "$(POSTGRES_PASSWORD)" in env["DATABASE_URL"]["value"] and env["WATCHOVER_DATASETS"]["value"] == "/datasets"
    assert "WATCHOVER_DEMO" not in env and env["WATCHOVER_API_URL"]["value"] == "http://watchover:8501" and env["MCP_API_KEY_WRITE"]["value"] == "0"          # live tools via the dashboard
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert c[probe]["httpGet"] == {"path": "/health", "port": "mcp"}, probe
    sec = by[("Secret", "watchover-mcp")]
    assert sec["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep" and len(base64.b64decode(sec["data"]["mcp-api-key"])) == 48
    again = next(d for d in render("--set", "mcp.enabled=true") if d["kind"] == "Secret" and d["metadata"]["name"] == "watchover-mcp")
    assert again["data"] != sec["data"]                                                                   # random per fresh install
    route = by[("Route", "watchover-mcp")]["spec"]
    assert route["host"] == "watchover-mcp.apps.example.com" and route["to"]["name"] == "watchover-mcp" and route["port"]["targetPort"] == "mcp"
    assert route["tls"]["termination"] == "edge" and route["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"
    svc = by[("Service", "watchover-mcp")]["spec"]
    assert svc["ports"][0]["port"] == 8765 and svc["selector"]["app.kubernetes.io/component"] == "mcp"
    np_ = by[("NetworkPolicy", "watchover-mcp")]["spec"]
    assert np_["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "mcp" and np_["policyTypes"] == ["Ingress"]
    assert np_["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"] == {"network.openshift.io/policy-group": "ingress"}
    assert np_["ingress"][0]["ports"] == [{"port": 8765, "protocol": "TCP"}]
    assert np_["ingress"][1]["from"][0]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "agents"}
    pg = by[("NetworkPolicy", "watchover-postgresql")]["spec"]["ingress"][0]["from"]
    assert sorted(f["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for f in pg) == ["app", "mcp"]
    assert by[("PersistentVolumeClaim", "watchover-mcp-datasets")]["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert [x["name"] for x in by[("Deployment", "watchover")]["spec"]["template"]["spec"]["containers"]] == ["dashboard", "keyd", "receiver"]
    dash = {e["name"]: e for e in by[("Deployment", "watchover")]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert dash["MCP_API_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "watchover-mcp", "key": "mcp-api-key"}       # same key as in Docker's .env
    assert "MCP_API_KEY" not in {e["name"] for e in next(d for d in render() if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["env"]}
    app_in = by[("NetworkPolicy", "watchover-app")]["spec"]["ingress"]
    assert {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "watchover", "app.kubernetes.io/instance": "watchover", "app.kubernetes.io/component": "mcp"}}} in app_in[1]["from"]
    assert app_in[1]["ports"] == [{"port": 8501, "protocol": "TCP"}]
    own = render("--set", "mcp.enabled=true,mcp.existingSecret=my-mcp")                                  # your own Secret: none rendered
    assert not [d for d in own if d["kind"] == "Secret" and "mcp" in d["metadata"]["name"]]
    c = next(d for d in own if d["kind"] == "Deployment" and d["metadata"]["name"] == "watchover-mcp")["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"]: e for e in c["env"]}["MCP_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "my-mcp"
    ext = render("--set", "mcp.enabled=true,postgresql.enabled=false,externalDatabase.existingSecret=extdb")
    c = next(d for d in ext if d["kind"] == "Deployment" and d["metadata"]["name"] == "watchover-mcp")["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"]: e for e in c["env"]}["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {"name": "extdb", "key": "database-url"}


@pytest.mark.skipif(not HELM, reason="helm not installed")
def test_committed_manifest_matches_the_chart():
    fresh = render(*RENDER_ARGS)
    assert fresh == committed(), "deploy/openshift/rendered/watchover.yaml is stale: regenerate it (command in its header)"


def test_no_secret_value_in_chart_manifest_or_guide():
    from watchover.auth import INITIAL_PASSWORD
    files = [p for p in CHART.rglob("*") if p.is_file()] + [RENDERED]
    files += [p for p in (ROOT / "docs").glob("KURULUM_OPENSHIFT.*")]
    assert len(files) > 8
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert INITIAL_PASSWORD not in text, p
        for m in re.finditer(r"(?im)^\s*(password|postgres-password|key|token|secret)\s*:\s*(\S+)", text):
            assert m.group(2) in ('""', "''", "postgres-password", "database-url", "mcp-api-key") or m.group(2).startswith(("{{", "|")), (p, m.group(0))
    for d in committed():                                                              # env: secrets only by reference
        for _, spec, _ in pod_specs([d]):
            for c in spec["containers"]:
                for e in c.get("env", []):
                    if re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", e["name"]):
                        assert "valueFrom" in e and "value" not in e, (c["name"], e["name"])
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["postgresql"]["existingSecret"] == "" and "password" not in values["postgresql"]
    assert values["routes"]["tls"]["key"] == "" and "demo" not in values and values["llm"]["enabled"] is False


def test_rhel_vs_openshift_table_is_the_same_in_both_guides():
    """one parity table, word for word in both installation guides (and their HTML), covering every operational topic"""
    def section(name):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        start = text.index("RHEL sunucu vs OpenShift: aynı ürün, iki kurulum")
        body = text[text.index("\n", start) + 1:]
        return body[:body.index("\n## ")].strip()
    docker, openshift = section("KURULUM_DOCKER.md"), section("KURULUM_OPENSHIFT.md")
    assert docker == openshift
    rows = [l.split("|")[1].strip() for l in docker.splitlines() if l.startswith("| ") and not l.startswith("| Konu")]
    for topic in ("Kurulum", "Yükseltme", "Geri alma", "Otomatik boyutlandırma", "Ayrı sıcak / soğuk disk", "PostgreSQL", "LLM (yalnız CPU)",
                  "MCP sunucusu", "Yalnız gerçek veri", "Yedek", "Ajan kayıt adresi", "TLS", "İzleme / sağlık", "Sorun giderme"):
        assert topic in rows, topic
    for html in ("KURULUM_DOCKER.html", "KURULUM_OPENSHIFT.html"):
        assert "RHEL sunucu vs OpenShift" in (ROOT / "docs" / html).read_text(encoding="utf-8"), html



def test_release_bump_moves_the_chart_and_the_rendered_manifest(tmp_path):
    """A release writes the new version into Chart.yaml appVersion (the default image tag) and into the oc-apply manifest's
    version labels and image tags, so an OpenShift install never pulls the previous version."""
    from watchover.release import sync_chart
    (tmp_path / "deploy" / "openshift" / "watchover").mkdir(parents=True); (tmp_path / "deploy" / "openshift" / "rendered").mkdir()
    (tmp_path / "deploy" / "openshift" / "watchover" / "Chart.yaml").write_text('name: watchover\nversion: 0.1.0\nappVersion: "1.2.3"\n')
    (tmp_path / "deploy" / "openshift" / "rendered" / "watchover.yaml").write_text(
        'app.kubernetes.io/version: "1.2.3"\nimage: reg/watchover/watchover:1.2.3\nimage: reg/watchover/watchover-receiver:1.2.3\nimage: reg/pg/postgresql-16:1.2.3\n')
    sync_chart("1.3.0", tmp_path)
    assert 'appVersion: "1.3.0"' in (tmp_path / "deploy" / "openshift" / "watchover" / "Chart.yaml").read_text()
    r = (tmp_path / "deploy" / "openshift" / "rendered" / "watchover.yaml").read_text()
    assert 'version: "1.3.0"' in r and "watchover:1.3.0" in r and "watchover-receiver:1.3.0" in r and "postgresql-16:1.2.3" in r   # other images untouched


def test_keyd_sidecar_alone_mounts_the_keys():
    """WP2 (I17): the keys PVC is mounted only by the keyd container; the dashboard reaches it through a Unix socket and keeps
    the vault key on a volume of its own, outside /data."""
    for name, docs in manifests():
        dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "watchover")
        cs = {c["name"]: c for c in dep["spec"]["template"]["spec"]["containers"]}
        vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
        assert vols["keys"]["persistentVolumeClaim"]["claimName"] == "watchover-keys", name
        mounts = {c: {m["name"] for m in cs[c].get("volumeMounts", [])} for c in cs}
        assert [c for c, m in mounts.items() if "keys" in m] == ["keyd"], name
        assert "data" not in mounts["keyd"] and {"keyd-sock", "secrets"} <= mounts["dashboard"], name
        env = {e["name"]: e.get("value") for e in cs["dashboard"]["env"]}
        assert env["WATCHOVER_KEYD"] == "unix:/run/keyd/keyd.sock" and env["WATCHOVER_VAULT_DIR"] == "/secrets", name
        assert "watchover.act.keyd" in cs["keyd"]["command"], name

