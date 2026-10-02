# Private data stays with the user

API keys come from environment variables or the one explicit configuration file documented in README.md.
The scripts do not search another project, agent history, browser profile or system credential store.
Journals and caches start empty and live under the current user's data directories, outside this repository.
They can contain full prompts, voice IDs and paths: do not upload them in bug reports.

Image prompts/references go to Laozhang; speech text goes to ElevenLabs. Motion renders locally;
optional music goes to ElevenLabs and optional ASR goes to OpenAI. Each operation is explicit.
Claude CLI preprocessing in the voice skill is optional and off by default; enabling a style sends that text to Claude.
No telemetry or delivery bot is part of these skills. Configuring public video-reference hosting makes those frames public.

Use GitHub's **Security → Report a vulnerability** for private disclosure. Do not put tokens or private
media into public issues. If a key was exposed, revoke it at the provider before cleaning history.
