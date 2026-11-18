# Code-review exercise: answers

The branch `review-exercise` holds a pull request, *"feat: easier onboarding and more resilient
syncs"* (description in `docs/REVIEW-EXERCISE-PR.md` on that branch). Every change sounds
reasonable, the unit tests pass, and it contains **six** problems a senior reviewer should
block. Review it first:

```bash
git fetch origin review-exercise
git diff main...origin/review-exercise
```

Write down each problem, why it matters, and what you'd ask for. Then open the answers.

<details>
<summary>1. Unlisted namespaces are now allowed (security hole)</summary>

`Policy.check` returns instead of raising when a namespace has no entry "so new teams don't
have to wait for a policy PR". Deny by default became **allow** by default: any namespace not
in the policy can ask the operator for ANY secret it can read. That is the confused-deputy hole
ADR 0002 exists to close.

**Ask for:** keep deny by default. Make onboarding faster with a template PR or a GitOps
generator, not by removing the check.
</details>

<details>
<summary>2. Secret permissions are now cluster-wide (security hole)</summary>

`deploy/operator.yaml` adds a ClusterRoleBinding to `secret-sync-secrets` "instead of one
RoleBinding per tenant". The operator can now read and write Secrets in **every** namespace,
`kube-system` included. A bug or a compromised dependency in a pod that already holds every
tenant's cloud secrets now also owns the cluster's own credentials.

**Ask for:** remove the ClusterRoleBinding; keep per-tenant RoleBindings (defence in depth with
the policy).
</details>

<details>
<summary>3. Secret values in the logs</summary>

`reconcile.fetch` gains `log.debug("fetched %s = %s", key, value)` "to debug rotation issues".
Debug gets enabled in production one day, and then every value lands in the log pipeline
(retention, search, other teams' access). The e2e check would catch it only if debug logging
were on.

**Ask for:** log keys, ids, hashes or lengths, never values. A content-hash change is enough to
debug rotations.
</details>

<details>
<summary>4. Partial Secrets (all-or-nothing broken)</summary>

`fetch` now skips values that fail "so one missing key doesn't block the others". A rotation
that fails halfway produces a Secret with the **new username and the old password** (or no
password at all): the application fails in confusing ways, at runtime, far from the cause.

**Ask for:** keep all-or-nothing. If one key is optional, make that explicit per key (an
annotation), not the default for all.
</details>

<details>
<summary>5. Takeover protection weakened (non-idempotent, unsafe update)</summary>

`owned_by` now only checks the `managed-by` label, not the ownerReference UID. Labels are
user-controlled: anyone who can edit Secrets in a namespace can add the label to a Secret
(say a TLS certificate) and the next ConfigMap that names it overwrites it. Two ConfigMaps
naming the same Secret now fight over it every resync (flapping, non-idempotent).

**Ask for:** keep the UID check: ownership is the ConfigMap's identity, not a label.
</details>

<details>
<summary>6. The tests that guarded these were changed or removed (missing tests)</summary>

`test_unlisted_namespace_is_refused` and `test_empty_policy_denies_everything` were deleted
("obsolete"), `test_all_or_nothing` now expects a partial write, and
`test_refuses_a_secret_owned_by_another_configmap` is gone. The suite is green because the
tests were made to agree with the bugs.

**Ask for:** restore them. A PR that deletes the test for the behaviour it changes deserves the
first and hardest question.
</details>

## What a good review comment looks like

> **Blocking (security):** `policy.py` L43 now allows any namespace that isn't in the policy.
> With one shared cloud identity that means any namespace can read any team's secrets (ADR
> 0002). Could we keep deny-by-default and speed up onboarding with a template PR instead?
> Happy to pair on it.

Say what is wrong, why it matters, what you'd like instead; mark what blocks the merge.
