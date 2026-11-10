# Contributing

Keep the skill portable: scripts and examples must work from a copied skill folder,
not depend on the maintainer's home directory. Use Python 3.10+ and the standard
library unless a concrete feature requires a documented dependency.

Run `python3 -m unittest discover -s tests -v` from the repository root. For behavior
changes, add a meaningful regression for the affected protocol/security boundary.
Use local fixtures; do not make tests depend on a real account, network tunnel or
private documents. Put diagnostics on stderr in stdio server modes.

Never commit `.local.json`, environment files, keys, cookies, native runtime logs
or user knowledge-base data. Review README/examples whenever CLI/config changes.
Preserve the distinction between configuration checks, local discovery, native
readiness and a real ChatGPT tool call. Add vendor examples only from current
official documentation and verify the actual installed help before live execution.

For new CLI tools, fix the executable/subcommand and parameter meaning. For HTTP,
fix destinations/methods and use credential references. Do not weaken redirect,
TLS, output, schema or default-write boundaries for convenience.
