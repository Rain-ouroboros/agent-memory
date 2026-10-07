# Contributing

Keep the core framework independent and free of implicit network/model calls. Changes to access, trust, provenance, revisions or context budgets need a regression test showing the user-visible boundary.

Use synthetic examples and temporary stores. Do not commit personal conversations, database files, logs, environment files, credentials or source-system snapshots. Capture trusted metadata through host adapters, not through text prefixes.

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/quickstart.py
python examples/agent_classes.py
```

Describe the behavior changed, the evidence used to validate it and remaining limits. New adapters belong behind explicit host configuration; do not add a provider dependency to the core to support one framework. Update both README languages when the public API or installation changes.
