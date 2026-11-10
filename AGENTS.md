# Repository guidance

Read README.md and the relevant references in skills/connect-local-tools before
changing behavior. Keep the runtime code inside that complete, copyable skill.

Validate with `python3 -m unittest discover -s tests -v`. Use offline fixtures and
never exercise real accounts merely to test a refactor. Preserve explicit user
authorization and the host's permission boundaries during live integration work.

Do not commit local configs, credentials, runtime state/logs or private documents.
Document exact verification scope. A mocked client or ready=true is not evidence
of a working ChatGPT connection. Keep English/Chinese READMEs and examples aligned.
