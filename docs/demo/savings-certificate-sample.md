# VSR Savings Certificate — sample (SYNTHETIC traffic)

This is a **sample** Savings Certificate produced by the REAL engine
(`mvp.learning.savings.savings_certificate`) running against LIVE infrastructure
(the `scverify` DynamoDB in us-east-1), over a **seeded, synthetic** tenant-day of
traffic. It is a provenance-guaranteed demo asset — NOT a hand-written mock, and
NOT a real tenant's audited number.

**Why it is synthetic.** The certificate's whole value is that it is a *proof* over
a tenant's real billed traffic. With no onboarded tenant yet, this sample seeds a
realistic mix (VSR advised a cheaper model and the tenant did not follow → saving;
VSR advised a dearer model → escalation loss, subtracted not hidden; tenant
followed the VSR → no delta) so a reader can see the exact shape and honesty of the
output. The `traffic: synthetic` provenance is stamped on the certificate itself
and the CLI prints it as a loud banner, so this can never be mistaken for an
audited number.

**How to reproduce a real one.** Onboard a tenant behind Stratoclave in
passthrough+shadow (the VSR is consulted and logged, execution stays on the client
pin), then:

```
python -m mvp.learning.savings_cli --tenant <tenant_id> --day YYYYMMDD
```

The output is identical in shape, without the SYNTHETIC banner, and with
`quality.measured` still `false` until a tenant-defined eval confirms routing
quality (no saving should be externally claimed before then). See
`docs/design/vsr-savings-certificate.md`.

## The sample output

```
=== VSR Savings Certificate: tenant demo-savings-* day 20260720 ===
  *** TRAFFIC: SYNTHETIC — SEEDED SAMPLE, NOT A REAL AUDITED TENANT NUMBER ***
  rate version:             builtin-defaults
  priced requests (base):   4
  billed over priced base:  $0.315500
  total billed (all reqs):  $0.333500
  NET saving:               $0.178000
    (+ cheaper-if-followed: $0.240000)
    (- dearer-if-followed:  $0.062000)
  net saving vs priced base: 56.4%
  request classes:          counterfactual=4, followed=2
  quality measured:         False (fill from tenant eval + VSR quality signal)
  scope:                    VSR-acted requests only (non-steered traffic is not in this certificate)
```

## Reading it (what makes it a certificate, not a dashboard estimate)

- **NET is the headline, and it can be negative.** `net = cheaper-if-followed −
  dearer-if-followed`. The escalation loss ($0.062) is SUBTRACTED, never hidden —
  a certificate that can show a loss is one a buyer trusts to show a gain.
- **Model-vs-model at one rate snapshot.** Each request is priced for BOTH the
  billed model and the VSR-suggested model over the SAME real billed tokens, at the
  SAME versioned rate (`rate version` is stamped so a re-run reproduces the number).
  This removes the rate-drift and cache-asymmetry biases that a naive
  `billed − estimate` would fold in VSR-favourably.
- **Honest denominator + class census.** Every request is classified
  (`counterfactual` / `followed` / …) and named in `request classes`; only the
  `counterfactual` class is in the savings base. Nothing is silently counted as
  zero saving.
- **Quality is separate and unmeasured here.** `quality measured: False`. The money
  saving and the routing quality are different claims; the second needs a tenant
  eval before any external comparative claim.

This is what LiteLLM structurally cannot produce: it asserts a cheaper route, it
does not prove one against your own billed tokens at ledger precision.
