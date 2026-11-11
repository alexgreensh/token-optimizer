# Comparison group

`TOKEN_OPTIMIZER_HOLDOUT` controls the fraction of sessions assigned to the comparison group. It defaults to `0` (off). Set a number between `0` and `1` in the environment inherited by the Token Optimizer hooks, then start new sessions. For example, `TOKEN_OPTIMIZER_HOLDOUT=0.1` assigns about 10% of sessions to the comparison group. Existing assignments are stored per session; the comparison is observational and does not prove that an individual command would otherwise have been re-run.
