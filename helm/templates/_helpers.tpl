{{- define "shurly.chart" -}}
{{ .Chart.Name }}
{{- end -}}

{{- define "shurly.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name -}}
{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}
{{ printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "shurly.labels" -}}
helm.sh/chart: {{ include "shurly.chart" . }}
app.kubernetes.io/name: {{ include "shurly.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{ include "shurly.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{- define "shurly.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "shurly.selectorLabels" -}}
app.kubernetes.io/name: {{ include "shurly.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "shurly.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "shurly.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{- define "shurly.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "shurly.env" -}}
{{- if .Values.env }}
{{- range $k, $v := .Values.env }}
  - name: {{ $k }}
    value: {{ $v | quote }}
{{- end }}
{{- end }}
{{- if .Values.secretRef }}
  - name: SHURLY_DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secretRef.name }}
        key: {{ .Values.secretRef.databaseKey | default "shurly_database_url" }}
  - name: SHURLY_ADMIN_TOKEN
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secretRef.name }}
        key: {{ .Values.secretRef.adminTokenKey | default "shurly_admin_token" }}
{{- end }}
{{- end -}}

{{- define "shurly.resources" -}}
{{- toYaml .Values.resources | nindent 2 }}
{{- end -}}

{{- define "shurly.probes" -}}
{{- if .Values.livenessProbe.enabled }}
  livenessProbe:
    httpGet:
      path: {{ .Values.livenessProbe.path }}
      port: http
    initialDelaySeconds: {{ .Values.livenessProbe.initialDelaySeconds }}
    periodSeconds: {{ .Values.livenessProbe.periodSeconds }}
    timeoutSeconds: {{ .Values.livenessProbe.timeoutSeconds }}
    failureThreshold: {{ .Values.livenessProbe.failureThreshold }}
{{- end }}
{{- if .Values.readinessProbe.enabled }}
  readinessProbe:
    httpGet:
      path: {{ .Values.readinessProbe.path }}
      port: http
    initialDelaySeconds: {{ .Values.readinessProbe.initialDelaySeconds }}
    periodSeconds: {{ .Values.readinessProbe.periodSeconds }}
    timeoutSeconds: {{ .Values.readinessProbe.timeoutSeconds }}
    failureThreshold: {{ .Values.readinessProbe.failureThreshold }}
{{- end }}
{{- end -}}
