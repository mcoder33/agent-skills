import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/orchestrate/scripts"
sys.path.insert(0, str(SCRIPTS))
import subscription_quota as quota


class SubscriptionQuotaTest(unittest.TestCase):
    def test_claude_reads_limits_and_ignores_spend_and_activity(self):
        windows = quota.parse_claude_usage("""
You are currently using your subscription to power your Claude Code usage
Current session: 13% used · resets Nov 20, 5:49pm (UTC)
Current week (all models): 85% used · resets Nov 24, 9:59am (UTC)
Current week (Sonnet only): 100% used · resets Nov 26, 9:59am (UTC)
Extra usage: 0% used
Tools: 50% used
""")
        self.assertEqual(windows, [
            {"window": "5h", "remaining_pct": 87, "reset": "Nov 20, 5:49pm (UTC)"},
            {"window": "7d", "remaining_pct": 15, "reset": "Nov 24, 9:59am (UTC)"},
            {"window": "7d:Sonnet only", "remaining_pct": 0, "reset": "Nov 26, 9:59am (UTC)"},
        ])

    def test_claude_rejects_stale_errored_and_incomplete_reports(self):
        full = "Current session: 13% used\nCurrent week (all models): 85% used"
        for report in (full + "\nShowing last-known usage from 5 minutes ago",
                       full + "\nCouldn't load usage data",
                       "Current session: 13% used",
                       "Current session: 999% used\nCurrent week: 85% used"):
            with self.subTest(report=report), self.assertRaises(quota.QuotaUnavailable):
                quota.parse_claude_usage(report)

    def test_kimi_reads_all_applicable_windows_and_ignores_extra_usage(self):
        windows = quota.parse_kimi_usage({
            "kind": "ok",
            "quota": {"usages": {
                "limit5h": {"usedRatio": 0.22, "resetAt": "2026-10-01T12:00:00Z"},
                "monthTotal": {"usedRatio": 1},
                "monthCode": {"usedRatio": 0.1234},
            }, "extraUsage": {"balanceCents": 500}},
        })
        self.assertEqual(windows, [
            {"window": "5h", "remaining_pct": 78, "reset": "2026-10-01T12:00:00Z"},
            {"window": "month_total", "remaining_pct": 0, "reset": None},
            {"window": "month_code", "remaining_pct": 87.66, "reset": None},
        ])

    def test_kimi_refuses_quota_from_a_different_default_provider(self):
        def request(path):
            self.assertEqual(path, "config")
            return {"default_model": "custom", "models": {"custom": {"provider": "elsewhere"}}}

        with self.assertRaisesRegex(quota.QuotaUnavailable, "default model"):
            quota.kimi_windows(request)

    def test_failed_probe_preserves_other_provider_and_hides_raw_error(self):
        with patch.object(quota, "claude_usage", side_effect=subprocess.TimeoutExpired(
                ["command", "private-arg"], 1)), patch.object(
                quota, "kimi_usage", return_value=[{"window": "5h", "remaining_pct": 55}]):
            result = json.dumps(quota.read_quotas(None, None))
        self.assertNotIn("private-arg", result)
        data = json.loads(result)
        self.assertEqual(data["claude"]["status"], "unknown")
        self.assertEqual(data["kimi"]["windows"][0]["remaining_pct"], 55)


if __name__ == "__main__":
    unittest.main()
