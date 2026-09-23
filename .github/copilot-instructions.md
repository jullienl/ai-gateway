# Project instructions: AI Gateway

Keep additions concise and rule-shaped.

## Customer-facing documentation and image publication

- **Customer docs first:** README and public guides target operators and
  customers. Show how to use the published GHCR container image before any
  source build or image-publishing procedure.
- **Maintainer procedures stay private:** source builds, GHCR publishing,
  release-tag mechanics, and registry troubleshooting belong in the ignored
  `docs/Private/` maintainer documentation, not in public customer guides.
- **Do not publish private runbooks:** keep maintainer-only material under
  `docs/Private/`, which must remain excluded from the public repository.