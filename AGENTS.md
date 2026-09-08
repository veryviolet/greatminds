# Release requirements

Before every release, audit README.md and all public documentation against the
actual current product. This is a standing user requirement. Review onboarding,
CLI examples, UI labels and screenshots, supported executors, runtime lifecycle,
configuration, version claims and links. Update affected pages in the same release.
README.md is the PyPI description; all its links must work outside GitHub.

Follow docs/recipes/cutting-a-release.md. Run the documentation checks and strict
site build before pushing a release tag. Passing automated checks does not replace
the semantic documentation audit. Never claim publication before verifying PyPI.

Do not leave temporary test web servers or daemons running. The user starts their
project web interface themselves. Do not stage .codex-solo-handoff/.
