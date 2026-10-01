# Remove the default defect-group cap

Accepted 2026-10-01. Amends ADR-0013's distinct-group bound; finding groups
remain required, bounded integers and advisory claims to the human reader.

The default four-group cap rejected confirmed Cal.com findings and moved some
into residual risk. Disclosing four groups and raising the cap to eight did
not establish a coverage gain. Removing the separate cap, while keeping ten
total finding entries, recovered the Office365 destination defect in every
repetition of both the explicit-four and shipped hidden-four comparisons.

The direct shipped-prompt comparison used three fresh paired repetitions on
six frozen source snapshots, including Rito before and after repair: 36 full
reviews, all accepted and source-assessed. Known target delivery rose from
18/24 to 21/24, with the same additional Cal.com target in all three repetitions.
Summed review time fell 5.57%. Both arms still had one false structured
Grafana attribution, missed its overlapping-build target, and gave some
incomplete repair advice. Repeated, nonexhaustive targets are not a general
accuracy estimate; removing the cap does not establish correctness of claims.

`review.max_distinct_groups` now ships as `null`. The verifier already supports
an absent or null cap, so its enforcement logic is unchanged. Consumers may
still configure a numeric cap. Generated artifact constraints state the
effective limit, keeping the model's instructions aligned with verification.

The ten-entry maximum, required group field, integer range, provenance and
output protections remain enforced. A group never authorizes or expands a
remediation command. Tests exercise the shipped ten-group/ten-entry boundary
and rejection at eleven entries, plus numeric consumer caps and malformed
cap configuration. Earlier experiment failures from tests that assumed a
shipped numeric cap remain in the local evidence; these assertions now test
the intended default and explicitly configured limits separately.

Raw trials and source assessments remain private local evidence under
`/home/ec2-user/aceiro-eval-runs/production-no-cap-20261001` and
`/home/ec2-user/aceiro-eval-runs/prompt-experiments-20261001`.
