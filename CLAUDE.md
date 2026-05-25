# Maintainer guidance for Claude (and humans)

This repo is a personal AWS management tool. The most important rules live
at the top of this file. Read them before making any changes — especially
before committing.

---

## 1. Secrets policy: NEVER put credentials in source

**No exceptions. No "just for testing." No "I'll remove it before commit."**

The following must NEVER appear in any file tracked by this repo:

- AWS access key IDs (anything starting with `AKIA`, `ASIA`, `AGPA`, `AROA`, `AIDA`, `ANPA`, `ANVA`, `ASCA`)
- AWS secret access keys (40-character base64-ish strings)
- AWS session tokens
- AWS account IDs (12-digit numbers)
- IAM user names, role ARNs, or any account-identifying ARN
- API keys for any service (Slack, GitHub, Stripe, OpenAI, etc.)
- Private keys (`-----BEGIN ... PRIVATE KEY-----`)
- Database connection strings with embedded passwords
- Personal email addresses, real customer names, or other PII
- `.env` files, `.aws/credentials` contents, or any file from `~/.aws/`

Credentials come from the **standard boto3 credential chain only**:
`~/.aws/credentials` → environment variables → instance profile.
That chain is configured outside this repo and stays outside this repo.

If you need to test code that takes a credential, use a placeholder like
`AKIAIOSFODNN7EXAMPLE` (the literal AWS docs example value) and document
it as such.

## 2. Pre-commit vigilance

**Before every `git add` and every `git commit`, do this:**

```bash
git diff --cached                                      # review staged changes
git diff --cached | grep -iE 'AKIA|ASIA|aws_secret|secret_access|private_key|BEGIN.*KEY'
git diff --cached | grep -E '[0-9]{12}'                # bare account IDs
git diff --cached | grep -iE 'password|api_key|api-key|bearer '
```

If any of those greps return a hit, **stop**. Investigate. Do not commit until
you've verified each hit is intentional and safe (e.g., a documented example
value, not a real credential).

Don't trust your memory — secrets sneak in through:
- Debugging print statements you forgot to remove
- Copy-pasted curl examples that included your token
- Default values in argparse for "just my account"
- Hardcoded values added "temporarily" that became permanent
- Generated files (notebooks, logs, fixtures) included by `git add .`
- AI-generated code that helpfully embedded an example value

Prefer `git add <specific-file>` over `git add .` to make every inclusion
deliberate.

## 3. If you committed a secret anyway

1. **Rotate the credential immediately** in IAM (or whichever provider).
   Treat it as compromised — assume anyone who could see the commit (push
   target, anyone who pulled, anyone who scraped GitHub) now has the key.
2. Remove the secret from the working tree and commit that fix.
3. If the commit reached `origin` (especially a public remote): consider
   the leak permanent for history-rewriting purposes — focus on revocation,
   not on scrubbing history. Force-pushing a cleaned history doesn't help
   if it was already cloned, indexed by GitHub search, or scraped.
4. If you absolutely must rewrite history (and the repo is private/yours):
   use `git filter-repo` (not the older `filter-branch`). Coordinate with
   anyone else who has a clone.

## 4. Cost-aware AWS APIs

Some of the APIs this tool calls **cost real money** on each invocation.
When adding actions or editing existing ones, know what you're touching:

| API | Cost | Notes |
|---|---|---|
| AWS Cost Explorer (`ce:GetCostAndUsage`) | **$0.01 per request** | `action_cost_breakdown` makes 2 calls per run. Don't loop, don't cron without thinking. |
| AWS Pricing (`pricing:GetProducts`) | Free | But aggressively rate-limited. Use `ThreadPoolExecutor` with reasonable concurrency (10 is fine; 100 will throttle). |
| Most EC2 describe/list calls | Free | Standard read APIs. |
| Mutating EC2 calls | Free, but real-world impact | `StopInstances`, `ModifyInstanceAttribute`, etc. cause downtime. Always show a confirm prompt before calling. |

## 5. Code style

- See README.md for the action function pattern. Keep new actions consistent.
- Use `console.print()` everywhere; never `print()`. Rich markup is fine.
- Use `pick_from_menu()` for selection prompts (it supports cancellation).
- Use boto3 paginators (`get_paginator(...)`) for any listing operation that
  could return more than a page.
- Hardcode the `pricing` and `ce` clients to `us-east-1` — those services
  only exist there as endpoints. See existing examples.
- Don't add a comment that just restates what the code does. Add comments
  when the WHY is non-obvious — especially AWS API quirks (the Pricing API
  PriceList-of-JSON-strings thing is a classic example documented in
  `fetch_price`).

## 6. Testing changes

This repo has no automated tests; the tool is interactive. To validate
changes:

1. Smoke-test imports/help: `./aws-tool --help` should exit cleanly.
2. Try the menu, hit each affected action.
3. For mutating actions (resize), test on a low-cost instance first
   (`t4g.nano` or `t3.nano`).
4. If you're adding a new action that hits a new AWS service, verify the
   required IAM permission is documented in README.md.

## 7. Dependencies

- Keep `requirements.txt` minimal. Each dependency is something the user
  has to trust and the launcher has to install on first run.
- Pin to major versions (`boto3>=1.40`, not `boto3==1.40.3`) so security
  patches land automatically.
- Don't add deps that themselves require system libraries (e.g.,
  Pillow needs libjpeg) — the launcher's bootstrap should "just work"
  on a clean macOS.
