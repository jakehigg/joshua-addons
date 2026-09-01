{{/* The name every object carries, cut to the 63 characters a label allows.
     Built from the release name, so two addons installed in the same
     namespace never collide: `helm install hello ...` and
     `helm install another-addon ...` each get their own objects. */}}
{{- define "joshua-addon.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels every object carries. */}}
{{- define "joshua-addon.labels" -}}
app.kubernetes.io/name: {{ include "joshua-addon.fullname" . }}
app.kubernetes.io/part-of: joshua
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/* The selector labels, a stable subset of joshua-addon.labels. A selector
     must never change across a release, so it excludes the chart and app
     versions. */}}
{{- define "joshua-addon.selectorLabels" -}}
app.kubernetes.io/name: {{ include "joshua-addon.fullname" . }}
{{- end -}}

{{/* The image reference. `image.repository` names the addon's own image and
     is required: an addon has no default image, so the chart stops with a
     clear message rather than pull something wrong. An empty `image.tag`
     means the chart's appVersion, exactly as joshua-ai's chart works, because
     the chart version and every addon image tag ship together. */}}
{{- define "joshua-addon.image" -}}
{{- if not .Values.image.repository -}}
{{- fail "set image.repository to the addon's image, such as ghcr.io/jakehigg/joshua-addons-hello" -}}
{{- end -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
