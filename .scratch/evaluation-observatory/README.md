# Evaluation observatory — throwaway logic prototype

Question: can the evaluation state distinguish qualified delivery from attempted
delivery, express frozen-data uncertainty transparently, make evidence maturity
depend on hard faults and regime coverage, and keep timing changes as
recommendations rather than schedule mutations?

Run from the repository root:

`python .scratch/evaluation-observatory/run.py`

This is deliberately isolated, stdlib-only, and driven solely by frozen JSON
fixtures. It reports qualification-by-10:30 probability with a deterministic
two-sided Wilson 90% score interval, and reports qualified-delivery time only
conditional on qualified observations. The delivery-time interval is a clearly
labeled small-sample empirical range; rejected, failed, late, missed, and
pending records never supply a delivery duration.

Findings: four or six observations are not promotion thresholds. A protected
hard fault rejects even at four observations; missing required regimes remains
insufficient at six; diverse, consistent, non-inferior evidence only recommends
promotion. The 09:00 upstream-prefetch duration remains separate from the
user-visible wait beginning at the actual 09:45-cycle start.

Its confidence/uncertainty and promotion rules are **prototype-only**. Production
must decide weighting, dependence treatment, regime taxonomy, and thresholds
required before any user-facing recommendation is eligible.
