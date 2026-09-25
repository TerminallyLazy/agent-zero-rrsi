<p align="center"><img src="https://raw.githubusercontent.com/TerminallyLazy/agent-zero-rrsi/main/logo.png" width="192" height="192" alt="RRSI: a pixel agent inside recursive loops, ascending toward an amber pixel"></p>

# RRSI for Agent Zero

RRSI evolves Agent Zero's harness using reproducible tasks, measured policy tokens, and Google's regularized selection method. It keeps the model, task pack, independent graders, framework and evaluation environment fixed within each campaign. The original method is vendored at commit `be50316e1db05914068a973f322770ef08ed7ba1` under Apache 2.0.

This is a community integration, not a Google or Agent Zero endorsed implementation. Benchmark scores from the paper are not claims about this plugin.

## Installation and setup

Install the community ZIP through Agent Zero's plugin installer. Open RRSI, select an available immutable Agent Zero Docker image, run setup checks, then enable automatic operation. The Docker daemon must be available to the trusted framework controller. Evaluation containers never receive the daemon socket.

The plugin uses the framework's existing model libraries and standard Python libraries. It does not install packages at startup or from its lifecycle hooks. Its scientific engine uses the vendored package with an explicit Agent Zero provider bridge; the upstream Vertex client is not used. The standalone MCP companion has its own explicit, isolated installation instructions.

The dashboard exposes setup, campaign control, eligibility, cost, calibration, candidate decisions/diffs, version inspection and rollback. API requests retain Agent Zero's authentication and CSRF checks. Generated skills, tools and roles appear in this dashboard; Agent Zero's ordinary Skills and Agent Editor catalogs cannot represent per-conversation versions.

## Automatic operation

Once configured, the controller checks hourly and starts while Agent Zero is idle. The first campaign can use the bundled curated suite; later campaigns require at least ten newly eligible tasks. Foreground work pauses research between evaluation units. A campaign resumes from durable records after interruption; a changed frozen configuration requires a new campaign.

Starting defaults are 20 rounds, two candidates, two trials per task, and three unchanged-baseline evaluations for noise calibration. Regularization uses the pinned upstream defaults. These are Agent Zero starting defaults, not experimentally optimized settings. Upstream's discrete cosine schedule is preserved exactly, including its rounding behavior.

`daily_budget_usd: 0` means unlimited monetary spending. A positive cap requires configured, conservative input and output prices for every paid role. Each call reserves its maximum estimated cost before dispatch. Unknown or interrupted usage is retained as unsettled; it never becomes zero-cost selection evidence. OAuth subscription providers may not report monetary prices, so a positive cap requires an explicit pricing policy for those roles.

Search calls, utility calls, subagents and policy calls have separate receipts. Selection uses complete evaluation policy usage; total search and evaluation spending remains visible separately.

## Learning and task eligibility

Only completed future foreground interactions are considered. Capture happens locally before any research-model call. Raw conversation prose and attachments are not stored by this plugin. Potentially sensitive, external-effect and unreproducible interactions remain ineligible, with metadata explaining why.

The automatic replay compiler currently recognizes bounded integer-array sorting, summing and deduplication requests. It creates fixtures and computes expected outputs independently of the assistant's answer. These families have permanently separate evolve, held-out and transfer assignments. Other interactions require a curated task pack with reproducible fixtures and an independent oracle. An unsupported request is never silently assigned a fabricated score.

The curated suite covers coding, files, structured data, documents, context management, tool recovery, local skill procedures and delegation. Hidden Python cases execute in separate disposable grader containers. Expected answers stay in the controller, outside the candidate's filesystem. Held-out and transfer results are evaluated after evolution and do not enter proposer feedback or selection.

## Versioned activation

The editable surface is a manifest plus plugin-owned payloads: prompts, control flow, configuration, output handling, context management, tools, skills, memory and subordinate roles. Stable native shims load version-qualified modules. Published payloads live outside watched `extensions` directories.

A conversation records its harness version; subordinate agents inherit it. Existing and restored conversations keep that version after publication. Missing or incompatible payloads produce an explicit recovery state. A completed campaign's final incumbent can be activated only after compatibility and functional canaries pass. Activation switches one atomic pointer and retains a rollback receipt.

**Native activation runs generated Python with Agent Zero's normal permissions. Docker protects evaluation; it does not sandbox production Python.** Enabling automatic activation accepts that trust boundary. A runtime initialization or execution failure requests rollback of the global pointer. Affected pinned conversations stay in recovery rather than silently changing behavior mid-conversation.

## Scientific provenance

`vendor/PROVENANCE.json` records original module hashes and the integration patch inventory. The plugin reuses upstream `Run`, `Domain`, proposal, analysis, critic, calibration, history, classification, evaluation and selection. Pruning remains an evaluated proposal directive; the controller does not unconditionally delete components.

Explicit adaptations are provider injection, complete measured history and diff access, fail-closed context capacity and usage checks, durable state/Git recovery, and bounded filesystem access. Deployment checks remain separate from scientific selection.

Sources: [pinned code](https://github.com/google-research/rrsi/tree/be50316e1db05914068a973f322770ef08ed7ba1), [paper website](https://regularized-rsi.com/). The companion `rrsi-paper` skill documents the supplied 24-page paper, including conversion limitations and source inconsistencies.

## Stop, disable and uninstall

Pause yields between work units; stop terminates owned work and preserves recovery evidence. Disabling the plugin causes its supervisor to stop owned workers. Uninstall runs cleanup and retains `usr/rrsi` for recovery/audit. It does not delete ordinary chats, user assets or unrelated Docker resources. Remove retained RRSI state only after all pinned conversations have been retired or recovered.

Compatibility and release acceptance are recorded externally with the distribution. Use a framework revision tested by that report; differing revisions are not assumed compatible. The local development checkout and discovered Docker image require separate checks.
