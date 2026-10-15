{{/* Names, labels, images and the shared security settings of every Watchover workload. */}}

{{- define "watchover.fullname" -}}
{{- if contains "watchover" .Release.Name -}}{{ .Release.Name | trunc 40 | trimSuffix "-" }}{{- else -}}{{ printf "%s-watchover" .Release.Name | trunc 40 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "watchover.labels" -}}
app.kubernetes.io/name: watchover
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: watchover
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* selector labels of one component: app | postgresql | ollama | backup */}}
{{- define "watchover.selector" -}}
app.kubernetes.io/name: watchover
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "watchover.image" -}}
{{- $tag := .root.Values.image.tag | default .root.Chart.AppVersion -}}
{{- printf "%s/%s/%s:%s" .root.Values.image.registry .root.Values.image.project .name $tag -}}
{{- end -}}

{{- define "watchover.dbSecret" -}}
{{- .Values.postgresql.existingSecret | default (printf "%s-db" (include "watchover.fullname" .)) -}}
{{- end -}}

{{- define "watchover.pgHost" -}}
{{- printf "%s-postgresql" (include "watchover.fullname" .) -}}
{{- end -}}

{{- define "watchover.ollamaUrl" -}}
{{- printf "http://%s-ollama:11434" (include "watchover.fullname" .) -}}
{{- end -}}

{{/* restricted-v2: no fixed UID / GID (the SCC assigns them), non-root, default seccomp */}}
{{- define "watchover.podSecurity" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "watchover.containerSecurity" -}}
allowPrivilegeEscalation: false
runAsNonRoot: true
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* settings the platform pins (watchover.settings.pinned): retention, the LLM lock, the public address */}}
{{- define "watchover.pinnedSettings" -}}
{{- $s := dict "live_retention_h" .Values.retention.liveRetentionH "cold_retention_days" .Values.retention.coldRetentionDays
      "hot_max_gb" .Values.retention.hotMaxGb "cold_max_gb" .Values.retention.coldMaxGb "history_retention_days" .Values.retention.historyRetentionDays -}}
{{- if .Values.routes.ui.host -}}
{{- $_ := set $s "public_host" (printf "https://%s" .Values.routes.ui.host) -}}
{{- end -}}
{{- if .Values.llm.lock -}}
{{- if .Values.llm.enabled -}}
{{- $_ := set $s "llm_provider" "ollama" -}}
{{- $_ := set $s "llm_base" (include "watchover.ollamaUrl" .) -}}
{{- $_ := set $s "llm_model" .Values.llm.model -}}
{{- $_ := set $s "llm_embed" .Values.llm.embedModel -}}
{{- $_ := set $s "llm_key" "" -}}
{{- else -}}
{{- $_ := set $s "llm_base" "" -}}
{{- $_ := set $s "llm_model" "" -}}
{{- $_ := set $s "llm_embed" "" -}}
{{- $_ := set $s "llm_key" "" -}}
{{- $_ := set $s "x_llm_probed" true -}}
{{- end -}}
{{- end -}}
{{- if not .Values.trainer.enabled -}}
{{- $_ := set $s "train_auto" false -}}
{{- end -}}
{{- range $k, $v := .Values.settings -}}
{{- $_ := set $s $k $v -}}
{{- end -}}
{{- $s | toJson -}}
{{- end -}}

{{/* the TLS block of every Route: edge termination, http redirected to https, optional company certificate */}}
{{- define "watchover.routeTls" }}
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
    {{- with .Values.routes.tls }}
    {{- if .certificate }}
    certificate: |
      {{- .certificate | nindent 6 }}
    key: |
      {{- .key | nindent 6 }}
    {{- end }}
    {{- if .caCertificate }}
    caCertificate: |
      {{- .caCertificate | nindent 6 }}
    {{- end }}
    {{- end }}
{{- end }}
