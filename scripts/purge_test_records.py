#!/usr/bin/env python3
"""
Round 8 (#6): Purge test-artifact records from the HardTruth daemon ledger.

Test runs use conversation IDs such as test-conv-*, auth-test-*, premise-auth-*,
baseline-auth-* and tier2-live-* (see DEFAULT_PREFIXES). The suite is now
token-less by default (so it never reaches the daemon), but past or manual live
runs leave artifacts in the physical HMAC-chained ledger. This ops tool rebuilds
the ledger without those records while preserving chain validity.

Usage:
    HARDTRUTH_DAEMON_LEDGER=/path/to/daemon_ledger.jsonl \\
    HARDTRUTH_DAEMON_KEY=/path/to/daemon_hmac.key \\
    python3 scripts/purge_test_records.py [--prefix test-conv-] [--prefix auth-test-] ...
    python3 scripts/purge_test_records.py --dry-run   # show what would be purged

Notes:
    - Stop the daemon first (this rebuilds the ledger file in place).
    - Same-user filesystem access only; deliberately NOT exposed over HTTP so a
      remote token holder cannot erase ledger evidence.
    - Default prefixes cover the current test namespaces. Documented adversarial
      probes (spotcheck-*, forge-test-*, fix-verify-*) are intentionally NOT
      defaulted; pass --prefix explicitly to remove those too.
"""
import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from daemon.ledger import DaemonLedger  # noqa: E402

DEFAULT_PREFIXES = [
    "test-conv-", "test-hybrid-", "test-gate-", "test-conv", "test_conv_http",
    "test-bypass-", "test-debug-",
    "auth-test-", "tier2-live-", "premise-auth-", "baseline-auth-",
    "breaker-", "bypass-", "chat-conv-", "commit-run-", "dead-daemon-",
    "doc-", "fail-conv-", "fake-pass-", "grep-", "informal-", "inline-conv-",
    "multi-fail-", "npm-res-", "poison-commit-", "prompt-conv-", "rule1-",
    "slide-", "spoof-", "spoof-multiline-", "stub-conv-", "success-conv-",
    "survive-", "taint-", "evade-backtick-", "evade-quote-",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--prefix", action="append", default=[],
        help="Conversation-ID prefix to purge (repeatable). Defaults: " + ", ".join(DEFAULT_PREFIXES) + " ...",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be purged without modifying the ledger.",
    )
    args = parser.parse_args()

    prefixes = args.prefix or DEFAULT_PREFIXES
    ledger = DaemonLedger()
    if args.dry_run:
        counts = ledger.count_records_by_prefix(prefixes)
        print("DRY RUN - no changes made.")
        print(
            f"would_purge={counts.get('purged', 0)} involved_conv_ids="
            f"{counts.get('conv_ids', 0)} total_records={counts.get('total', 0)}"
        )
        return 0
    result = ledger.purge_records(prefixes)
    print(
        f"purged={result.get('purged')} kept={result.get('kept')} "
        f"status={result.get('status', 'ok')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())