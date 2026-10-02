# Working on this repository

This file is for agents and people changing the skill itself. To *use* the skill, read SKILL.md; to install it, README.md.

- Keep credentials, voice registries, prompts, client media, generated files and journals out of Git.
  User data lives in `~/.config/media-skills/`, `~/.local/share/<skill>/` and `~/.cache/<skill>/`.
- Paths are relative to this checkout or come from the documented environment variables — never hard-code a home directory.
- Tests use synthetic fixtures and mocked providers: no paid API call is needed to validate a change.
  Run before publishing: `.venv/bin/python -m unittest discover -s tests -v`.
- Keep vendored third-party files with their licenses and attribution.
- Instructions in SKILL.md and references/ are written in Russian; keep new sections in the same language and style.
