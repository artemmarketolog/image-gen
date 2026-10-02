# Your files and history

Default base: `${XDG_DATA_HOME:-~/.local/share}/image-gen`, overridable with `IMAGE_GEN_DATA_DIR`.

| Location below the base | Contents |
|---|---|
| `image-gen/ledger.jsonl` | Request status, full prompt, parameters and returned path |
| `image-gen/prompts/YYYY-MM.md` | Readable prompt history without a project |
| `image-gen/images/` | Permanent generated images without a project |
| `creatives/<project>/images/` | Project images in unique directories |
| `creatives/<project>/prompts.md` | Project prompt history |
| `video-gen/` | Optional generated-video jobs and journal |

Temporary copies and the single-process lock live under `${XDG_CACHE_HOME:-~/.cache}/image-gen`.
New installations contain no previous user's prompts, images or request IDs.
The repository's `.gitignore` is a second guard; do not move private history into the checkout.
