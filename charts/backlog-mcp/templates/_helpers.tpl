{{/* Expand and validate the chart name. */}}
{{- define "backlog-mcp.name" -}}
{{- $name := default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- include "backlog-mcp.dnsLabel" (list $name "nameOverride") -}}
{{- end }}

{{/* Fail closed when schema validation is explicitly skipped. */}}
{{- define "backlog-mcp.requireBool" -}}
{{- $value := index . 0 -}}
{{- $field := index . 1 -}}
{{- if not (kindIs "bool" $value) -}}
{{- fail (printf "%s must be a boolean" $field) -}}
{{- end -}}
{{- end }}

{{/* Render an integer only after applying its required bounds. */}}
{{- define "backlog-mcp.intRange" -}}
{{- $value := int (index . 0) -}}
{{- $field := index . 1 -}}
{{- $minimum := int (index . 2) -}}
{{- $maximum := int (index . 3) -}}
{{- if or (lt $value $minimum) (gt $value $maximum) -}}
{{- fail (printf "%s must be between %d and %d" $field $minimum $maximum) -}}
{{- end -}}
{{- $value -}}
{{- end }}

{{/* Reject legacy annotations that can weaken the fixed container profiles. */}}
{{- define "backlog-mcp.validatePodAnnotations" -}}
{{- range $key, $_ := . -}}
{{- if or (hasPrefix "container.apparmor.security.beta.kubernetes.io/" $key) (eq $key "seccomp.security.alpha.kubernetes.io/pod") (hasPrefix "container.seccomp.security.alpha.kubernetes.io/" $key) -}}
{{- fail (printf "podAnnotations[%q] cannot override the chart security profiles" $key) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/* Validate a Kubernetes DNS label used by labels and selectors. */}}
{{- define "backlog-mcp.dnsLabel" -}}
{{- $name := index . 0 -}}
{{- $field := index . 1 -}}
{{- if or (gt (len $name) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $name)) -}}
{{- fail (printf "%s must be a valid Kubernetes DNS label" $field) -}}
{{- end -}}
{{- $name -}}
{{- end }}

{{/* Validate a DNS-1035 label used by the chart's Service and shared object name. */}}
{{- define "backlog-mcp.dns1035Label" -}}
{{- $name := index . 0 -}}
{{- $field := index . 1 -}}
{{- if or (gt (len $name) 63) (not (regexMatch "^[a-z]([-a-z0-9]*[a-z0-9])?$" $name)) -}}
{{- fail (printf "%s must be a valid DNS-1035 label beginning with a letter" $field) -}}
{{- end -}}
{{- $name -}}
{{- end }}

{{/* Validate every component of a Kubernetes DNS subdomain object reference. */}}
{{- define "backlog-mcp.dnsSubdomain" -}}
{{- $name := index . 0 -}}
{{- $field := index . 1 -}}
{{- if or (gt (len $name) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$" $name)) -}}
{{- fail (printf "%s must be a valid Kubernetes DNS subdomain" $field) -}}
{{- end -}}
{{- $name -}}
{{- end }}

{{/* Create and validate a release-qualified name. */}}
{{- define "backlog-mcp.fullname" -}}
{{- $fullname := "" -}}
{{- if .Values.fullnameOverride }}
{{- $fullname = .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- $name := include "backlog-mcp.name" . -}}
{{- if contains $name .Release.Name }}
{{- $fullname = .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- $fullname = printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}
{{- include "backlog-mcp.dns1035Label" (list $fullname "fullnameOverride") -}}
{{- end }}

{{/* Labels used by every chart resource. */}}
{{- define "backlog-mcp.labels" -}}
helm.sh/chart: {{ include "backlog-mcp.chart" . | quote }}
{{ include "backlog-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end }}

{{/* Immutable labels used by the Deployment selector. */}}
{{- define "backlog-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "backlog-mcp.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end }}

{{/* Chart name and version for labels. */}}
{{- define "backlog-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Application images must use an immutable digest. */}}
{{- define "backlog-mcp.image" -}}
{{- $digest := required "image.digest is required; use an immutable image manifest digest" .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}
{{- fail "image.digest must be a lowercase sha256 manifest digest" -}}
{{- end -}}
{{- printf "%s@%s" .Values.image.repository $digest -}}
{{- end }}

{{/* Helm test images also use an immutable digest. */}}
{{- define "backlog-mcp.testImage" -}}
{{- $digest := .Values.test.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}
{{- fail "test.image.digest must be a lowercase sha256 manifest digest" -}}
{{- end -}}
{{- printf "%s@%s" .Values.test.image.repository $digest -}}
{{- end }}

{{/* Service account name. */}}
{{- define "backlog-mcp.serviceAccountName" -}}
{{- include "backlog-mcp.requireBool" (list .Values.serviceAccount.create "serviceAccount.create") -}}
{{- $name := "" -}}
{{- if .Values.serviceAccount.create -}}
{{- $name = default (include "backlog-mcp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- $name = required "serviceAccount.name is required when serviceAccount.create is false" .Values.serviceAccount.name -}}
{{- end -}}
{{- include "backlog-mcp.dnsSubdomain" (list $name "serviceAccount.name") -}}
{{- end }}

{{/* The pre-existing Secret with HTTP Basic accounts. */}}
{{- define "backlog-mcp.authSecretName" -}}
{{- $name := default (printf "%s-auth" (include "backlog-mcp.fullname" .)) .Values.auth.existingSecret -}}
{{- include "backlog-mcp.dnsSubdomain" (list $name "auth.existingSecret") -}}
{{- end }}

{{/* The key containing the Basic account list. */}}
{{- define "backlog-mcp.authSecretKey" -}}
{{- $key := required "auth.existingSecretKey is required" .Values.auth.existingSecretKey -}}
{{- if or (gt (len $key) 253) (not (regexMatch "^[A-Za-z0-9._-]+$" $key)) -}}
{{- fail "auth.existingSecretKey must be a valid Kubernetes Secret data key" -}}
{{- end -}}
{{- $key -}}
{{- end }}

{{/* The PVC selected for SQLite storage. */}}
{{- define "backlog-mcp.pvcName" -}}
{{- $name := default (include "backlog-mcp.fullname" .) .Values.persistence.existingClaim -}}
{{- include "backlog-mcp.dnsSubdomain" (list $name "persistence.existingClaim") -}}
{{- end }}
