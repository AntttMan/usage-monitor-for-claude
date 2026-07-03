# API Reference

Example responses from the Anthropic OAuth API endpoints used by the app. These serve as implementation reference - field names, data types, and structure.

> [!NOTE]
> These are real-world examples with anonymized data, last updated in July 2026. Fields may change without notice as these are undocumented internal endpoints. If your API response contains fields not listed here, please open an issue with an anonymized example so we can keep this reference up to date.

## /api/oauth/usage

```
https://api.anthropic.com/api/oauth/usage
```

```json
{
  "five_hour": {
    "utilization": 20.0,
    "resets_at": "2026-07-03T15:00:00.000000+00:00",
    "used_dollars": 4.2,
    "remaining_dollars": 16.8,
    "limit_dollars": 21.0
  },
  "seven_day": {
    "utilization": 60.0,
    "resets_at": "2026-07-07T00:00:00.000000+00:00",
    "used_dollars": 90.0,
    "remaining_dollars": 60.0,
    "limit_dollars": 150.0
  },
  "seven_day_oauth_apps": null,
  "seven_day_opus": null,
  "seven_day_sonnet": null,
  "seven_day_cowork": null,
  "iguana_necktie": null,
  "extra_usage": {
    "is_enabled": true,
    "monthly_limit": null,
    "used_credits": 5090.0,
    "utilization": null,
    "currency": "USD",
    "decimal_places": 2,
    "disabled_reason": null,
    "daily": null,
    "weekly": null
  },
  "limits": [
    {
      "kind": "session",
      "group": "session",
      "percent": 20,
      "severity": "normal",
      "resets_at": "2026-07-03T15:00:00.000000+00:00",
      "scope": null,
      "is_active": false
    },
    {
      "kind": "weekly_all",
      "group": "weekly",
      "percent": 60,
      "severity": "warning",
      "resets_at": "2026-07-07T00:00:00.000000+00:00",
      "scope": null,
      "is_active": false
    },
    {
      "kind": "weekly_scoped",
      "group": "weekly",
      "percent": 85,
      "severity": "critical",
      "resets_at": "2026-07-07T00:00:00.000000+00:00",
      "scope": {
        "model": { "id": null, "display_name": "Fable" },
        "surface": null
      },
      "is_active": true
    }
  ],
  "spend": {
    "enabled": true,
    "used": { "amount_minor": 5090, "currency": "USD", "exponent": 2 },
    "cap": null,
    "limit": null,
    "balance": { "amount_minor": 25000, "currency": "USD", "exponent": 2 },
    "percent": 0,
    "severity": "normal",
    "auto_reload": null,
    "can_purchase_credits": false,
    "can_toggle": false,
    "disabled_reason": null,
    "disclaimer": "Usage credits cover you when you hit your plan limits."
  },
  "member_dashboard_available": false
}
```

### Notes on the `limits` array

Per-model weekly limits (e.g. the weekly limit for a specific model such as Fable) are reported in the `limits` array as entries with `kind: "weekly_scoped"` and a non-null `scope.model.display_name`. They are **no longer** exposed as flat `seven_day_<model>` fields - those keys are now `null`. The `session` and `weekly_all` entries duplicate the top-level `five_hour` and `seven_day` fields.

The app derives a synthetic top-level field for each model-scoped entry, named `<period>_<model>` (`session` -> `five_hour_<model>`, `weekly` -> `seven_day_<model>`, e.g. `seven_day_fable`), carrying `{ "utilization": <percent>, "resets_at": <resets_at> }`. This lets scoped limits flow through the same auto-detection, labeling, and alerting as the flat quota fields.

### Notes on usage credits

The popup's "Usage credits" section reads `extra_usage.used_credits` (spend so far), `extra_usage.monthly_limit` (`null` = unlimited, otherwise a cap), and `spend.balance` (current prepaid balance, shown when present). `extra_usage` amounts are plain numbers of minor currency units (cents for a 2-decimal currency). `spend` amounts (`used`, `balance`, and, when set, `cap`/`limit`) are money objects of the form `{ "amount_minor": <int>, "currency": <str>, "exponent": <int> }`.

## /api/oauth/profile

```
https://api.anthropic.com/api/oauth/profile
```

```json
{
  "account": {
    "uuid": "...",
    "full_name": "Max Clau",
    "display_name": "Max",
    "email": "max@clau.de",
    "has_claude_max": true,
    "has_claude_pro": false,
    "created_at": "2024-10-22T07:21:47.099776Z"
  },
  "organization": {
    "uuid": "...",
    "name": "max@clau.de's Organization",
    "organization_type": "claude_max",
    "billing_type": "stripe_subscription",
    "rate_limit_tier": "default_claude_max_5x",
    "has_extra_usage_enabled": true,
    "subscription_status": "active",
    "subscription_created_at": "2026-01-16T18:22:42.826732Z"
  },
  "application": {
    "uuid": "...",
    "name": "Claude Code",
    "slug": "claude-code"
  }
}
```

## /api/oauth/organizations/{org_uuid}/prepaid/credits

```
https://api.anthropic.com/api/oauth/organizations/{org_uuid}/prepaid/credits
```

The prepaid credit balance is not present in `/api/oauth/usage` or `/api/oauth/profile` (there `spend.balance` is `null`). It lives behind the organization UUID from the profile response above. Same domain, same OAuth token, read-only. `amount` is the balance in minor currency units (cents for a 2-decimal currency); `balance_credits` is the same value in whole currency units.

```json
{
  "amount": 25000,
  "currency": "USD",
  "balance_credits": 250,
  "auto_reload_settings": null,
  "pending_invoice_amount_cents": null,
  "last_paid_purchase_cents": null,
  "expiry_policy_months": null
}
```
