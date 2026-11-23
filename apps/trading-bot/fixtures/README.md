# Frozen regression baselines

These two historical engine modules are test fixtures, not additional services.
They preserve independent pre-change implementations for the existing delivery
and chart regression tests. Their original hashes are recorded in the root
`docs/source-provenance.json`. Tests compare decisions and state while explicitly
excluding the metadata/config changes under examination.
