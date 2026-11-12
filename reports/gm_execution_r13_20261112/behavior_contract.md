# R13 GM data and execution contract

GM_CONSUMED_INPUT_CACHE_V1_20261112 persists the exact effective, normalized,
merged 24000-M1 window before committing an initialization checkpoint. The
checkpoint binds immutable parquet bytes, actual contract, window and provenance.
Original seed and journal references remain. Live callback rows still use the
existing committed journal. Restart validates both and rejects changed, missing,
revised or insufficient consumed input. Historical known_at remains UNKNOWN.

Legacy checkpoint migration is explicit and evidence-bound: exact frozen seed,
committed journal prefix and the actually consumed initialization cache must
reproduce the old first-boot checkpoint state before adding gm_input_cache.
No runtime, last_eob, evidence IDs, tombstone, intent or pause fields are reset.
No incoming cache alone is permission to bypass the old missing-prefix guard.

GM_PRIORITY_HANDOFF_V1_20261112 services actual GM feeds before per-product
analysis, rotates the GM and secondary groups separately, and gives the groups
separate bounded queue budgets. Callback quotes are replaceable latest state,
one per symbol, with original event/receipt clocks and out-of-order rejection.
The backup poll publishes per-symbol current-run mailboxes instead of unlimited
quote events. It remains a fallback and cannot replace a newer subscribed tick.
No bars, candidate events, RPCs or orders are conflated. Warmup callback bars are
retained with their original clocks and exact identities, then replayed through
the existing closed-minute book after CORE_READY. Revision/overflow fails closed.

CLOCK_DEGRADED_RETRY_V1_20261112 schedules another dual-source probe within 5s
after a retryable degraded probe instead of sleeping another 30s. It never
changes the original sample timestamp, 90s lifetime, source quorum, offset,
disagreement or RTT checks. Persistent packet loss can still stop new entries.

PAPER_ENTRY_V5 / PAPER_EXEC_V5 and technical core remain unchanged. Quote,
account and service 3s; signal original 30s; clock 90s / 0.5s / 0.1s / 1s;
position, sizing, margin, daily loss and holding gates remain unchanged.
PA/divergence replacement remain SHADOW. TQ remains isolated and secondary.
Expired signals are never renewed. Host liveness or PA completeness cannot
substitute for actual production-core progress.

Validation separately records software regression, isolated real process
restart, native final-call regression with synthetic SDK, installed bytes,
user-started actual host load and natural submit/fill/exit. Synthetic SDK calls
are never reported as broker orders. Release installation retains pause/halt.
User alone stops and starts the client. No hot cover or automatic rollback.
