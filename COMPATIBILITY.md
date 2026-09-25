# Compatibility and installation boundary

Version 0.1.4 uses Agent Zero's native plugin, function-extension, model, skill, tool-policy and conversation serialization contracts. It includes direct root/web UI branding assets, a compact native UI and a read-only model-price suggestion endpoint. Its grader inherits the shared bounded Docker logger: two 1 MB files, compatible with the local driver's default compression. This corrects the earlier single-file override that prevented grading containers from starting. Frozen campaigns require replacement after the grader-source change; their original evidence remains valid as historical records.

The implementation has passed native behavioral checks against these separately captured framework revisions:

- `6a6cecff8527b164668c7a6ab2f76b6b1ed7cfa1`
- `b1cbd1f960a1a5c4482b324dcff4742aa67b7a51`

Both ran in an immutable Linux ARM64 image with framework Python 3.12.4. Text tools, native Responses tools, local embeddings and brokered remote embeddings were exercised. Other operating systems, architectures and framework revisions require their own checks. This is compatibility evidence, not a claim that an accepted harness transfers between revisions. Each artifact is bound to its campaign's exact framework revision; a changed or unavailable Git identity requires recovery.

Install the root-level plugin ZIP using Plugins → Install → ZIP. Setup requires an existing Docker CLI/daemon and an immutable Agent Zero framework image. Select an Evaluation image in Settings and run Check setup before enabling automatic operation. The picker lists local Docker images and prefers the current controller image after a bounded runtime check; a verified official Agent Zero image is the fallback. The probe is unprivileged, networkless, read-only, resource-limited and has no mounts or provider credentials. It never pulls images. Its recommendation is not a full campaign compatibility certification. Settings retain custom or unavailable image references and do not change until saved. The framework controller needs Docker access. Candidate containers receive neither the Docker socket nor provider credentials.

No dependency installation occurs during startup or lifecycle hooks. The plugin uses Agent Zero's existing Python dependencies and the bundled Apache-licensed RRSI implementation. Install the optional MCP companion in its separately documented isolated Python environment.

Stop or disable RRSI before manually replacing its files. Source drift stops campaigns instead of changing their frozen experiment. Uninstall terminates owned workers and retains recovery artifacts. Back up `usr/rrsi` before relocating an installation with pinned conversations.

Accepted generated Python executes with ordinary Agent Zero permissions. Automatic activation is a trust decision, not a sandbox guarantee.

Version 0.1.4 image discovery was checked against the live Linux ARM64 controller at revision `b1cbd1f960a1a5c4482b324dcff4742aa67b7a51`. Focused tests cover recommendation fallback, Docker failures, cleanup, selection preservation, native POST handling and CSRF rejection. Adding the image API changes the frozen plugin-source hash; older unfinished campaigns retain their evidence and require a new campaign after the update.
