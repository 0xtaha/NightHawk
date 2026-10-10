{{- define "nighthawk.labels" -}}
app.kubernetes.io/part-of: nighthawk
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* The label NetworkPolicies select on: the workload's name in the network contract. */}}
{{- define "nighthawk.component" -}}
nighthawk.io/component: {{ . }}
{{- end -}}

{{/* Name of the Secret the operator synchronizes for one workload and one secret reference. */}}
{{- define "nighthawk.secretName" -}}
{{- printf "vault-%s-%s" .workload .ref -}}
{{- end -}}

{{/* A workload entry by name. */}}
{{- define "nighthawk.workload" -}}
{{- $found := dict -}}
{{- range .root.Values.workloads -}}{{- if eq .name $.name -}}{{- $found = . -}}{{- end -}}{{- end -}}
{{- toYaml $found -}}
{{- end -}}

{{/* A projected volume that lays one workload's secrets out as the command-line tool materializes them: <path>/<key>. */}}
{{- define "nighthawk.secretsVolume" -}}
{{- $workload := include "nighthawk.workload" . | fromYaml -}}
- name: secrets
  projected:
    defaultMode: 0440
    sources:
    {{- range $workload.secrets }}
      - secret:
          name: {{ include "nighthawk.secretName" (dict "workload" $.name "ref" .ref) }}
          items:
            - key: {{ .key }}
              path: {{ .path }}/{{ .key }}
    {{- end }}
{{- end -}}

{{/* Pods reach the gateway by the names its certificate carries. */}}
{{- define "nighthawk.gatewayAliases" -}}
{{- with .Values.hostAliases }}
hostAliases: {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "nighthawk.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 65532
  runAsGroup: 65532
  fsGroup: 65532
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "nighthawk.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
{{- end -}}


{{/* One NetworkPolicy peer. `direction` is `from` or `to`. */}}
{{- define "nighthawk.peer" -}}
{{- $peer := .peer -}}
{{- if eq $peer.kind "component" }}
{{ .direction }}:
  - namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: {{ $peer.namespace }}
    podSelector:
      matchLabels:
        nighthawk.io/component: {{ $peer.component }}
{{- else if eq $peer.kind "all-pods" }}
{{ .direction }}:
  - namespaceSelector: {}
    podSelector: {}
{{- else if eq $peer.kind "dns" }}
{{ .direction }}:
  - namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: kube-system
    podSelector:
      matchLabels:
        k8s-app: kube-dns
{{- else }}
{{- /* Nodes and callers outside the cluster have no label to select on. */ -}}
{{ .direction }}:
  - ipBlock: {cidr: 0.0.0.0/0}
  - ipBlock: {cidr: "::/0"}
{{- end }}
{{- end -}}
