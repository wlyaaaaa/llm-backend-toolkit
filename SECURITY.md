# Security policy

## Secrets

Pass credentials through environment variables or another caller-owned secret reference. Never commit credentials, authorization headers, `.env` files, private application configuration, or captured provider requests.

## Local data

Job state and artifacts are local runtime data and must remain outside Git. Cloud media transfer requires an explicit `privacy.cloud_allowed=true` request. Source and attachment paths are accepted only from the top-level caller; backend models never choose arbitrary local paths.

## Resource isolation

Use the machine's managed Ollama endpoint. LocalOCR and ChineseASR must use their existing managed wrappers. Do not bypass the machine GPU broker or terminate active workloads.

## Reporting

Report vulnerabilities privately to the repository owner before public disclosure. Do not attach real credentials, private prompts, personal media, OCR output, or transcripts to a public issue.
