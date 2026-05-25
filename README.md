# aws-tool

Personal, menu-driven AWS management tool built on `boto3` + `rich`.

It's a single Python entry point with a small registry of "actions" — each
action is a function that takes a `region` and prints rich-formatted output.
Add more actions as you find more repetitive AWS chores worth automating.

## Quick start

```bash
# One-time: AWS credentials (interactive; sets ~/.aws/credentials and ~/.aws/config)
aws configure

# Run the tool. First run auto-creates .venv/ and installs requirements.txt.
./aws-tool
./aws-tool --region us-west-2
```

## File layout

| Path | What it is |
|---|---|
| `aws-tool` | Bash launcher. Bootstraps `.venv/` on first run, then `exec`s `aws_tool.py`. |
| `aws_tool.py` | All the Python: helpers, action functions, main menu loop. |
| `requirements.txt` | `boto3`, `rich`. Pinned to major versions. |
| `.venv/` | Auto-created by the launcher. Gitignored. |
| `CLAUDE.md` | Guidance for Claude (and humans) maintaining this repo. **Read it.** |

## Prerequisites

- **Python 3.10+** (uses `X | None` union syntax)
- **AWS CLI** configured (`aws configure`) — the script reads credentials from
  the standard locations (`~/.aws/credentials`, env vars, instance profile, etc.)
- **AWS region** with the resources you care about (default: `us-east-1`)

## IAM permissions

The script makes read-heavy calls plus a few mutating calls for resize. Minimum
policy if you want a scoped IAM user instead of using root credentials:

```
ec2:DescribeInstances
ec2:DescribeInstanceTypes
ec2:DescribeInstanceTypeOfferings
ec2:DescribeImages
ec2:StopInstances
ec2:StartInstances
ec2:ModifyInstanceAttribute
pricing:GetProducts
ce:GetCostAndUsage
```

`pricing:*` and `ce:*` only exist as global service endpoints in `us-east-1`,
which is why those boto3 clients are pinned to `us-east-1` in code regardless
of the user-selected region.

## Actions

| # | Action | Notes |
|---|---|---|
| 1 | Show monthly spend breakdown | Cost Explorer API. **Costs $0.01 per call** (script makes 2 calls per run). Data lags ~24h. |
| 2 | List EC2 instances | Read-only. Free. |
| 3 | Resize an EC2 instance | Drill-down: category → family → size with live prices. Stop → modify type → start. Public IP changes unless EIP is attached. |

## Adding a new action

```python
# aws_tool.py

def action_list_eips(region: str) -> None:
    """List all Elastic IPs and whether they are associated."""
    ec2 = boto3.client('ec2', region_name=region)
    addrs = ec2.describe_addresses()['Addresses']
    # ... build a rich Table and console.print it
    # Return when the action is done; main loop comes back to the menu.

ACTIONS = [
    ("Show monthly spend breakdown", action_cost_breakdown),
    ("List EC2 instances",           action_list_instances),
    ("Resize an EC2 instance",       action_resize_instance),
    ("List Elastic IPs",             action_list_eips),    # <-- new
]
```

Conventions to follow:
- The function signature is `(region: str) -> None`.
- Return on completion or cancellation — don't `sys.exit()`. The main loop
  re-displays the menu.
- Use `console.print()` (the module-level `rich` Console), not `print()`.
- Use `pick_from_menu()` for selection prompts. It supports cancellation
  ("0) Back") so the user can bail out without exiting the tool.
- Wrap mutating AWS calls in clear "before/after" status prints so a user
  watching the terminal knows what's happening.

## Maintenance notes

- **Architecture filtering** in the resize action prevents picking incompatible
  families (e.g., resizing a `t4g` arm64 instance only shows arm64-compatible
  families). AWS would error otherwise, but failing client-side is friendlier.
- **Pricing fetches are parallelized** with `ThreadPoolExecutor`. Per-type API
  calls take 200–500 ms each; a family with 20 sizes serial = 10+ seconds.
  boto3 clients are thread-safe for read operations.
- **Stop → modify → start** is the only path that actually changes instance
  type. Hibernate would preserve RAM (and tmux) but doesn't allow type changes
  on resume. Use `tmux-resurrect` if you want layout restored.

## Security

See [CLAUDE.md](./CLAUDE.md). The short version:

- **Never** put AWS access keys, secrets, account IDs, or any credential in
  source. Credentials live in `~/.aws/` only.
- Before every commit, `git diff` and grep for `AKIA`, `ASIA`, `aws_secret`,
  and anything that looks base64-y.
- If you accidentally commit a key, rotate it immediately in IAM and force-push
  the cleaned history.
