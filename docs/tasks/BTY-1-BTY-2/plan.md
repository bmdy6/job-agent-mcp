# BTY-1 / BTY-2

Scope: Liepin reply source and shared monitor diagnostics/alerts. No other platform browser access, deployment or schema change.

1. Add bounded, content-free step diagnostics to Liepin fetch; preserve login/risk/space exception semantics and original failures during cleanup. Test each stage and summary field drift.
2. Persist per-platform failure stage/reason, consecutive count and last success; deduplicate per reason/channel until recovery, retry failed notifications. Expose runtime diagnostics through setup_check. Test restart, reason changes, recovery and privacy.
3. Pause launchd before up to three low-volume Liepin-only read polls; stop on risk/login challenges. Restore launchd in finally and confirm fresh production polling. Record evidence and limitations.
4. Run full mcp unittest suite and build, review final diff, open private draft PR referencing BTY-1/BTY-2; comment links on Linear and write result.md. Claude owns merge/deploy.
