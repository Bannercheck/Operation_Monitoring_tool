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
    "everything": ["--set", "llm.enabled=true,trainer.enabled=true,backup.enabled=true,llm.ollama.allowEgress=false,demo=false",
                   "--set", "routes.ui.host=w.apps.example.com,routes.ingest.host=wi.apps.example.com"],
    "official-postgres": ["--set", "postgresql.flavor=official,postgresql.image=docker.io/library/postgres:16,backup.enabled=true"],
    "external-db": ["--set", "postgresql.enabled=false,externalDatabase.existingSecret=extdb,llm.enabled=true,llm.lock=false"],
}
INGEST_PATHS = {"/ingest", "/enroll", "/agent.py", "/agent/install.sh", "/mobile", "/rum"}


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
    assert env["WATCHOVER_DEMO"]["value"] == "0"
    assert claims[mounts[env["WATCHOVER_LOG_HOT_DIR"]["value"]]].endswith("-logs-hot")
    assert claims[mounts[env["WATCHOVER_LOG_COLD_DIR"]["value"]]].endswith("-logs-cold")
    assert mounts["/data"] == "data" and {m["mountPath"]: m["name"] for m in recv["volumeMounts"]}["/data"] == "data"
    assert "secretKeyRef" in env["POSTGRES_PASSWORD"]["valueFrom"] and "$(POSTGRES_PASSWORD)" in env["DATABASE_URL"]["value"]
    assert [p["containerPort"] for p in dash["ports"]] == [8501] and [p["containerPort"] for p in recv["ports"]] == [8600]
    assert dash["livenessProbe"]["httpGet"]["path"] == "/api/health" and recv["livenessProbe"]["httpGet"]["path"] == "/health"
    assert app["spec"]["strategy"]["type"] == "Recreate" and spec["enableServiceLinks"] is False
    pvcs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "PersistentVolumeClaim"}
    assert pvcs[claims["logs-hot"]]["spec"]["resources"]["requests"]["storage"] == "50Gi"
    assert pvcs[claims["logs-cold"]]["spec"]["resources"]["requests"]["storage"] == "50Gi"
    settings = __import__("json").loads(env["WATCHOVER_SETTINGS"]["value"])
    assert settings["live_retention_h"] == 48 and {"cold_retention_days", "hot_max_gb", "cold_max_gb"} <= set(settings)


@pytest.mark.parametrize("shape", ["rendered"] + (["everything"] if HELM else []))
def test_routes_edge_tls_and_ingest_paths(shape):
    docs = committed() if shape == "rendered" else render(*SHAPES[shape])
    routes = [d for d in docs if d["kind"] == "Route"]
    assert routes and all(r["spec"]["tls"]["termination"] == "edge" and r["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect" for r in routes)
    ingest = [r for r in routes if r["spec"]["port"]["targetPort"] == "ingest"]
    assert INGEST_PATHS <= {r["spec"].get("path") for r in ingest}
    ui = [r for r in routes if r["spec"]["port"]["targetPort"] == "http"]
    assert len(ui) == 1 and not ui[0]["spec"].get("path")


@pytest.mark.skipif(not HELM, reason="helm not installed")
def test_network_policies_and_llm_shape():
    docs = render(*SHAPES["everything"])
    np_ = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    pg = np_["watchover-postgresql"]["spec"]
    sources = [f["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for f in pg["ingress"][0]["from"]]
    assert sorted(sources) == ["app", "backup"] and pg["ingress"][0]["ports"] == [{"port": 5432, "protocol": "TCP"}]
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
            assert m.group(2) in ('""', "''", "postgres-password", "database-url") or m.group(2).startswith(("{{", "|")), (p, m.group(0))
    for d in committed():                                                              # env: secrets only by reference
        for _, spec, _ in pod_specs([d]):
            for c in spec["containers"]:
                for e in c.get("env", []):
                    if re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", e["name"]):
                        assert "valueFrom" in e and "value" not in e, (c["name"], e["name"])
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["postgresql"]["existingSecret"] == "" and "password" not in values["postgresql"]
    assert values["routes"]["tls"]["key"] == "" and values["demo"] is False and values["llm"]["enabled"] is False
