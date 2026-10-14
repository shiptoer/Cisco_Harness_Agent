# A3 — model pin enforcement

Date: 2026-10-14. Status: ĐỀ XUẤT (D-045).
Base: `4f533cbe4696383e2bff5da0de768e7db1115e12` (PR #3 merged).
Source ZIP SHA-256: `5ce10ce6d0ae7aa7f241f576791a05009c161799972625b6e9fac6e3fe04434c`.

## Problem and behavior

Previously the runner copied its pin into each patch and reported the model from
session logs, but a valid change could still be accepted when the reported model
was different. The existing negative-control test demonstrated that reporting
behavior without requiring an error outcome.

The runner now snapshots ModelConfig once before invoking dsh. Both stage patches
and validation use that same frozen object, even if model.toml is changed during
a run. Every completed stage used for a non-error outcome must supply fresh,
parseable request/header configuration with matching provider, model and
reasoningEffort. The existing config validator makes thinking consistent with
effort: off disables it; low/high/max enable it. There is no independent
provider-side thinking attestation.

A CBA mismatch stops before TSA or pending_review. A TSA mismatch stops before
render/candidate/change_package publication. B0 checks before candidate/package
publication too. Checks apply equally at B0–B4, without changing gate/sensor levels.

| Condition | Outcome |
| --- | --- |
| All reported headers match | Continue normal validation; this alone does not mean accept |
| Any complete reported header differs | error / MODEL_PIN_MISMATCH |
| Missing fresh header, incomplete config, malformed JSON evidence | error / MODEL_PIN_UNVERIFIED |
| Existing preflight, timeout, runtime error | Preserve its original error code |
| Leak scan incomplete, including unreadable compressed log | Preserve LEAK_SCAN_INCOMPLETE when no earlier error |

Error outcomes have no verdict or stopped_by. ERROR is never accuracy PASS.
The check is not placed in finish(), avoiding error/finish recursion. Existing
errors do not get rewritten merely because no model evidence was produced.

## Evidence handling

- Strict reader is separate from the legacy permissive request_configs helper.
- Parse JSON events, not substring matches; JSON-escaped request/header names work.
- Every header is checked, not just the first/last model. Missing required fields
  are errors, and long values are not truncated into a matching prefix.
- Fresh logs are read only under sessions/. A session path already present before
  a stage cannot supply that stage's evidence, even if rewritten. This also
  prevents retained CBA logs from masking absent TSA headers with keep_dsh_home.
- A fresh invocation must create fresh session paths. A future dsh release that
  resumes/reuses sessions requires an explicit adaptation and new tests.
- Trace includes model_pin_checked with stage, issue categories and check result;
  dsh_home_scanned retains the reported configs. result.model/reasoning_effort
  continue to report observed values rather than copying the pin.

## Scope and limitations

Session logs are written by the untrusted runtime. This detects observable
configuration drift and missing evidence, not a compromised runtime that forges
all matching headers. It does not attest which provider/model actually served a
network request. No OS isolation, deployment, API gateway or provider attestation
is introduced. No model API calls were made during this work.

No changes to locked models/, schemas, dependencies, workflows, IOS templates,
web approval tokens, evaluator, datasets or multi-vendor support.

## Verification

Final results and raw command logs are included in the delivery ZIP under
verification/. All model activity is simulated with fake_dsh; MCP server and
runner code execute normally. Tests use temporary directories.

New test module covers CBA/TSA mismatches, provider/effort mismatches, missing
headers with retained logs, malformed/escaped JSON, multiple headers, all four
thinking modes, a changed model.toml between stages, preflight/exit precedence,
pending_review/clarification and fixed enforcement across B0–B4. Negative controls
remove only pin_problem and must accept the otherwise valid mismatched request.
The original dropped-patch test now explicitly requires MODEL_PIN_MISMATCH.
The outcome-code suite includes producers for both added error codes.

Windows validation must be run after applying the patch. Linux/fake test success
is not a model accuracy score, a live dsh compatibility claim, or EVE-NG validation.

## A1 regression fixture compatibility

The first full-suite run exposed old A1 negative controls whose synthetic session
contained no request header. A3 correctly stopped them before gate replay, so they
no longer isolated the A1 control. The hostile fixture now also forges a matching
compressed request header. This deliberately models a stronger attacker and
keeps the independent gate replay tests meaningful: gate enabled must reject;
removing only gate replay must again accept. No security assertion was removed.
The fixture also demonstrates why matching runtime logs are not an attestation.


## Follow-up test corrections

The timeout-with-corrupt-trace test still requires TIMEOUT and all leak evidence;
its exact dropped-event list now includes the new model_pin_checked event.
A local HTTP approval test was initially blocked by the test environment's
network guard. The final guard permits literal loopback IPs only and continues
to block external TCP connections. No application HTTP/security behavior changed.
Both follow-up tests passed (2 passed in 1.43s) before the final full-suite run.

## Linux validation result

- Full suite: **1041 passed, 1 xfailed in 121.75s (0:02:01)**.
- New regression module: **43 passed in 130.59s**.
- A1/A2/A4 regression after fixture adjustment: **24 passed in 111.60s**.
- Ruff: **All checks passed!**
- Dry cases: **16/16 at each B0–B4; 80/80 expected outcomes**.
- Python 3.12.14 / Linux. Full command: `python -m pytest -n 6 --dist loadfile --tb=short`.
- External TCP blocked by test guard; localhost HTTP used only by local tests.
- No model API calls or deployment. Windows results are not claimed.

## Windows validation — 2026-10-14

- Targeted Ruff: All checks passed.
- Fixed explicit UTF-8 decoding when reading tsa.patch.yml in the snapshot test.
- Snapshot regression after correction: 1 passed in 6.35s.
- Full suite: 1040 passed, 1 skipped, 1 xfailed in 1227.57s.
- Results reported from the operator's Windows machine.
- No model API calls or deployment during validation.