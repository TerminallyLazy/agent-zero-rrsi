# Pinned RRSI source

Source: https://github.com/google-research/rrsi at `be50316e1db05914068a973f322770ef08ed7ba1`.
The original Apache-2.0 notices are retained. `PROVENANCE.json` records original and current hashes; `INTEGRATION.patch` contains every source change.

The adapter calls upstream `Run` and its native estimators, calibration, edit schedule, history statistics, component tagging, exploration, pruning directives and selection. Its integration patches provide model-role injection, complete proposal history and critic context, contained read-only trace tools, interruption propagation and durable state writes. Host shell access is disabled; read_file, grep and glob remain available. Formula modules are unchanged, and archived original-source outputs are compared exactly by the science reference test.

`helpers/engine.py` owns serialized operation journals and compare-and-restore Git recovery. `helpers/domain.py` owns frozen Agent Zero task execution and independent scoring. Their strict metering guards reject incomplete usage instead of allowing upstream's generic missing-cost fallback to influence selection.
