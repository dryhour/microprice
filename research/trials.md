# Trial log: `trials.csv`

Every configuration we evaluate goes into [trials.csv](./trials.csv), **including failures and dead ends**. The deflated Sharpe ratio (Bailey & López de Prado, 2014) and White's Reality Check / Hansen's SPA need the true number of trials and the spread of their results. A log that only keeps the winners makes both meaningless.

One row = one (feature or model) × (label) × (sample) evaluation. Append only; never edit or delete a row. If a row turns out to be wrong, add a new row and set the old one's status in the new row's `notes` (`supersedes T0012`).

| Column | Meaning |
|---|---|
| `trial_id` | `T` + zero-padded counter, e.g. `T0001`. Never reused. |
| `run_at_utc` | ISO 8601 UTC time the trial ran |
| `git_sha` | commit of the code that produced it (`-dirty` if uncommitted changes) |
| `owner` | `QR` or `QE` |
| `stage` | `eda`, `ic`, `regression`, `nested`, `model`, `backtest`, `robustness`, `holdout` |
| `hypothesis` | `H1`…`H5`, or `none` for exploratory rows |
| `feature` | feature column(s), `+`-joined; for models, the feature set name |
| `label` | label column, e.g. `y_10s` |
| `model` | `spearman`, `pearson`, `ols_hac`, `ridge`, … |
| `params` | JSON with everything else needed to rerun (window, lags, thresholds, fold spec) |
| `symbols` | `+`-joined symbols, or `ALL` for the full universe v1 |
| `dates` | `in_sample`, a fold name, or an explicit range |
| `sample` | `in_sample`, `wf_fold_k`, `oos`, or `holdout` |
| `n_obs` | rows used |
| `n_eff` | effective sample size after accounting for overlap (e.g. n_obs × sampling interval / h), if relevant |
| `metric` | name of the headline number: `ic_spearman`, `beta`, `r2_oos`, `sharpe_net`, … |
| `value` | the headline number |
| `t_stat`, `p_value` | test statistic and p-value (HAC-based where labels overlap) |
| `ci_low`, `ci_high` | confidence interval for `value` (method in `params`) |
| `sharpe_ann` | annualized Sharpe if the trial is a strategy, else empty |
| `status` | `ok`, `failed` (code or data problem), or `invalid` (later found to be wrong) |
| `notes` | free text |

Write rows with `research.studies.trials_log.append_trials()` so the columns and IDs stay consistent.
