#!/usr/bin/env python3
"""
aws-tool — personal AWS management tool.

Architecture:
    - Single-file CLI. The launcher script (`aws-tool` in this directory)
      bootstraps a local .venv on first run and execs this file.
    - Menu-driven: main() loops, showing the actions registered in ACTIONS.
    - Each action is a function `(region: str) -> None`. To add one, write
      the function then append `(label, fn)` to ACTIONS. See README.md.

Conventions:
    - All output goes through the module-level `console` (rich Console).
    - Mutating AWS calls are wrapped with status prints so the user sees
      what's happening; use waiters where AWS state changes asynchronously.
    - Per-action errors are caught in main() so the user returns to the menu
      instead of the tool dying mid-session.

Security:
    NEVER hardcode AWS access keys, secret keys, account IDs, or any
    credential material in this file. Credentials come from the standard
    boto3 chain (~/.aws/credentials, env vars, instance profile, etc.).
    See CLAUDE.md for the full secrets policy.
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.prompt import Confirm, IntPrompt
from rich.table import Table

console = Console()

# AWS Pricing API is one of the few services that doesn't accept region codes.
# It expects long-form location names like "US East (N. Virginia)" — these are
# the values it indexes products against. This table is just the regions we
# care about; extend as needed. AWS publishes the full list in their docs.
# If a region is missing, the resize menu shows "?" for prices instead of
# failing — see fetch_price().
REGION_TO_LOCATION = {
    'us-east-1':      'US East (N. Virginia)',
    'us-east-2':      'US East (Ohio)',
    'us-west-1':      'US West (N. California)',
    'us-west-2':      'US West (Oregon)',
    'eu-west-1':      'Europe (Ireland)',
    'eu-west-2':      'Europe (London)',
    'eu-central-1':   'Europe (Frankfurt)',
    'ap-northeast-1': 'Asia Pacific (Tokyo)',
    'ap-southeast-1': 'Asia Pacific (Singapore)',
    'ap-southeast-2': 'Asia Pacific (Sydney)',
    'ap-south-1':     'Asia Pacific (Mumbai)',
    'sa-east-1':      'South America (Sao Paulo)',
    'ca-central-1':   'Canada (Central)',
}

# ---------- shared helpers ----------

def pick_from_menu(prompt: str, options: list[str], allow_cancel: bool = True) -> int | None:
    """Print a numbered menu, return 0-based index, or None if user cancels."""
    for i, opt in enumerate(options, 1):
        console.print(f"  [bold]{i:>2})[/bold] {opt}")
    if allow_cancel:
        console.print("   [bold]0)[/bold] [dim]Back[/dim]")
    valid = [str(i) for i in range(0 if allow_cancel else 1, len(options) + 1)]
    choice = IntPrompt.ask(prompt, choices=valid, show_choices=False)
    if allow_cancel and choice == 0:
        return None
    return choice - 1


def fetch_price(pricing_client, instance_type: str, location: str) -> str | None:
    """Return Linux on-demand USD/hour for an instance type, or None.

    AWS Pricing API quirk: PriceList items are returned as **JSON-encoded
    strings** inside a JSON array, not parsed JSON. We have to json.loads()
    each item. The product schema is deeply nested:
        product.terms.OnDemand.<offer_id>.priceDimensions.<dim_id>.pricePerUnit.USD

    The offer_id and dim_id are opaque AWS identifiers, so we grab the first
    of each — there's always exactly one for on-demand Linux.

    The filter set narrows to "Linux, Shared tenancy, no preinstalled SW,
    used capacity" — the common case. Reserved instances, savings plans,
    Windows, etc. would need different filters.
    """
    try:
        resp = pricing_client.get_products(
            ServiceCode='AmazonEC2',
            Filters=[
                {'Type': 'TERM_MATCH', 'Field': 'instanceType',    'Value': instance_type},
                {'Type': 'TERM_MATCH', 'Field': 'location',        'Value': location},
                {'Type': 'TERM_MATCH', 'Field': 'operatingSystem', 'Value': 'Linux'},
                {'Type': 'TERM_MATCH', 'Field': 'tenancy',         'Value': 'Shared'},
                {'Type': 'TERM_MATCH', 'Field': 'preInstalledSw',  'Value': 'NA'},
                {'Type': 'TERM_MATCH', 'Field': 'capacitystatus',  'Value': 'Used'},
            ],
            MaxResults=1,
        )
        if not resp.get('PriceList'):
            return None
        product = json.loads(resp['PriceList'][0])
        on_demand = product['terms']['OnDemand']
        first_term = next(iter(on_demand.values()))
        first_dim = next(iter(first_term['priceDimensions'].values()))
        return first_dim['pricePerUnit']['USD']
    except (ClientError, KeyError, StopIteration, ValueError):
        return None


def list_instances(ec2) -> list[dict]:
    resp = ec2.describe_instances()
    return [i for r in resp['Reservations'] for i in r['Instances']]


def name_of(inst: dict) -> str:
    return next((t['Value'] for t in inst.get('Tags', []) if t['Key'] == 'Name'), "")


def style_state(state: str) -> str:
    return {
        'running':       '[green]running[/green]',
        'stopped':       '[red]stopped[/red]',
        'stopping':      '[yellow]stopping[/yellow]',
        'pending':       '[yellow]pending[/yellow]',
        'shutting-down': '[yellow]shutting-down[/yellow]',
        'terminated':    '[dim]terminated[/dim]',
    }.get(state, state)


# ---------- action: list EC2 instances ----------

def action_list_instances(region: str) -> None:
    ec2 = boto3.client('ec2', region_name=region)
    instances = list_instances(ec2)
    if not instances:
        console.print(f"[yellow]No EC2 instances in {region}.[/yellow]")
        return
    table = Table(title=f"EC2 instances in {region}")
    table.add_column("Name", style="cyan")
    table.add_column("ID")
    table.add_column("Type")
    table.add_column("State")
    table.add_column("Public IP")
    table.add_column("Launched")
    for inst in instances:
        table.add_row(
            name_of(inst) or "(no name)",
            inst['InstanceId'],
            inst['InstanceType'],
            style_state(inst['State']['Name']),
            inst.get('PublicIpAddress') or "—",
            inst['LaunchTime'].strftime('%Y-%m-%d'),
        )
    console.print(table)


# ---------- action: resize an EC2 instance ----------
#
# Flow:
#   1. list instances -> user picks one
#   2. show current type stats (vCPU, mem, network)
#   3. user picks category -> family -> size (drill-down keeps menus small)
#   4. fetch live prices for sizes in parallel (Pricing API)
#   5. confirm, then stop -> modify-instance-attribute -> start
#   6. print SSH connect string (guesses user from AMI name, key path from ~/.ssh)
#
# Important constraints:
#   - The instance must be 'running' or 'stopped' to resize. Other states
#     (pending, stopping, terminated) are rejected up front.
#   - Architecture filtering (x86_64 vs arm64) is done client-side so the
#     menu only shows compatible families. AWS would also error, but
#     pre-filtering is friendlier and avoids wasted clicks.
#   - Public IP changes on stop/start unless an Elastic IP is attached.

CATEGORIES = [
    ("General purpose       (t, m, a, mac)",          r'^(t|m|a|mac)[0-9]'),
    ("Compute optimized     (c)",                     r'^c[0-9]'),
    ("Memory optimized      (r, x, u, z)",            r'^(r|x|u|z)[0-9]'),
    ("Storage optimized     (i, d, h)",               r'^(i|d|h)[0-9]'),
    ("Accelerated / GPU     (g, p, inf, trn, dl, vt, f)", r'^(g|p|inf|trn|dl|vt|f)[0-9]'),
]


def _ssh_user_for_ami(ami_text: str) -> str:
    text = ami_text.lower()
    if 'ubuntu' in text:                          return 'ubuntu'
    if 'debian' in text:                          return 'admin'
    if 'centos' in text:                          return 'centos'
    if 'fedora' in text:                          return 'fedora'
    if 'bitnami' in text:                         return 'bitnami'
    if 'rocky' in text or 'almalinux' in text:    return 'rocky'
    return 'ec2-user'  # amzn linux, rhel, suse, default


def _find_key_path(key_name: str) -> str:
    home = Path.home()
    for candidate in [home / '.ssh' / f'{key_name}.pem', home / '.ssh' / key_name]:
        if candidate.exists():
            return str(candidate)
    return f"<path-to-your-{key_name or 'ssh'}-key>"


def action_resize_instance(region: str) -> None:
    ec2 = boto3.client('ec2', region_name=region)
    # Pricing is a global service with regional endpoint only in us-east-1
    # (and ap-south-1). Always use us-east-1 regardless of user's region.
    pricing = boto3.client('pricing', region_name='us-east-1')
    location = REGION_TO_LOCATION.get(region)

    # --- pick instance ---
    console.print(f"\n[dim]Fetching EC2 instances in {region}...[/dim]")
    instances = list_instances(ec2)
    if not instances:
        console.print(f"[yellow]No EC2 instances in {region}.[/yellow]")
        return

    table = Table(title=f"EC2 instances in {region}")
    table.add_column("#", justify="right")
    table.add_column("Name", style="cyan")
    table.add_column("ID")
    table.add_column("Type")
    table.add_column("State")
    for i, inst in enumerate(instances, 1):
        table.add_row(str(i), name_of(inst) or "(no name)",
                      inst['InstanceId'], inst['InstanceType'],
                      style_state(inst['State']['Name']))
    console.print(table)

    pick = pick_from_menu("Pick an instance", [i['InstanceId'] for i in instances])
    if pick is None:
        return
    sel = instances[pick]
    inst_id = sel['InstanceId']
    current_type = sel['InstanceType']
    current_state = sel['State']['Name']
    arch = sel['Architecture']
    ami_id = sel.get('ImageId', '')
    key_name = sel.get('KeyName', '')

    if current_state not in ('running', 'stopped'):
        console.print(f"[red]Instance is in state '{current_state}' — can only resize 'running' or 'stopped'.[/red]")
        return

    cur = ec2.describe_instance_types(InstanceTypes=[current_type])['InstanceTypes'][0]
    console.print(
        f"\n[bold]Current:[/bold] [cyan]{current_type}[/cyan]  "
        f"vCPU={cur['VCpuInfo']['DefaultVCpus']}  "
        f"mem={cur['MemoryInfo']['SizeInMiB']/1024:.1f} GiB  "
        f"net={cur['NetworkInfo']['NetworkPerformance']}  "
        f"state={style_state(current_state)}  arch={arch}"
    )

    # --- pick category ---
    console.print("\n[bold]Instance type categories[/bold]")
    cat_pick = pick_from_menu("Pick a category", [c[0] for c in CATEGORIES])
    if cat_pick is None:
        return
    pattern = re.compile(CATEGORIES[cat_pick][1])

    # --- pick family ---
    console.print(f"[dim]Fetching available types in {region}...[/dim]")
    paginator = ec2.get_paginator('describe_instance_type_offerings')
    available = set()
    for page in paginator.paginate(LocationType='region'):
        for o in page['InstanceTypeOfferings']:
            if pattern.match(o['InstanceType']):
                available.add(o['InstanceType'])
    if not available:
        console.print(f"[yellow]No types in this category for {region}.[/yellow]")
        return
    families = sorted({t.split('.')[0] for t in available})
    fam_pick = pick_from_menu("Pick a family", families)
    if fam_pick is None:
        return
    family = families[fam_pick]

    # --- pick size with live prices ---
    console.print(f"[dim]Fetching sizes and prices for {family}...[/dim]")
    sizes_resp = ec2.describe_instance_types(
        Filters=[{'Name': 'instance-type', 'Values': [f'{family}.*']}]
    )
    sizes = [t for t in sizes_resp['InstanceTypes']
             if arch in t['ProcessorInfo']['SupportedArchitectures']]
    sizes.sort(key=lambda t: (t['VCpuInfo']['DefaultVCpus'], t['MemoryInfo']['SizeInMiB']))
    if not sizes:
        console.print(f"[yellow]No {family} sizes compatible with {arch} in {region}.[/yellow]")
        return

    types_in_order = [s['InstanceType'] for s in sizes]
    prices: dict[str, str | None] = {t: None for t in types_in_order}
    # Parallelize price fetches: each Pricing API call is 200-500ms; a family
    # with 15-20 sizes serial = 5-10 seconds of waiting. boto3 clients are
    # thread-safe for read operations like get_products, so sharing one
    # client across threads is fine.
    if location:
        def _fetch(t: str) -> str | None:
            return fetch_price(pricing, t, location)
        with ThreadPoolExecutor(max_workers=10) as pool:
            for t, p in zip(types_in_order, pool.map(_fetch, types_in_order)):
                prices[t] = p

    table = Table(title=f"{family}.* (Linux on-demand, {region})")
    table.add_column("#", justify="right")
    table.add_column("Type", style="cyan")
    table.add_column("vCPU", justify="right")
    table.add_column("Mem (GiB)", justify="right")
    table.add_column("Network")
    table.add_column("USD/hr", justify="right", style="green")
    for i, s in enumerate(sizes, 1):
        t = s['InstanceType']
        marker = " [bold yellow](current)[/bold yellow]" if t == current_type else ""
        p = prices.get(t)
        price_str = f"${float(p):.4f}" if p else "?"
        table.add_row(
            str(i),
            f"{t}{marker}",
            str(s['VCpuInfo']['DefaultVCpus']),
            f"{s['MemoryInfo']['SizeInMiB']/1024:.1f}",
            s['NetworkInfo']['NetworkPerformance'],
            price_str,
        )
    console.print(table)

    size_pick = pick_from_menu("Pick new size", types_in_order)
    if size_pick is None:
        return
    new_type = types_in_order[size_pick]
    if new_type == current_type:
        console.print("[yellow]That's the current type. Nothing to do.[/yellow]")
        return

    # --- confirm and execute ---
    console.print(
        f"\n[bold]Resize plan:[/bold] {inst_id}  "
        f"[cyan]{current_type}[/cyan] -> [cyan]{new_type}[/cyan]"
    )
    console.print("Steps: stop -> modify type -> start. Public IP will change unless an Elastic IP is attached.")
    if not Confirm.ask("Proceed?", default=False):
        console.print("Cancelled.")
        return

    # EC2 state transitions are asynchronous. The waiters poll
    # DescribeInstances every ~15s until the target state is reached or a
    # timeout fires (default 40 attempts = ~10 min). If a waiter times out
    # it raises WaiterError, which bubbles up to main() and the user is
    # returned to the menu with the instance left in whatever state it's in.
    if current_state == 'running':
        console.print(f"[dim]Stopping {inst_id}...[/dim]")
        ec2.stop_instances(InstanceIds=[inst_id])
        ec2.get_waiter('instance_stopped').wait(InstanceIds=[inst_id])
        console.print("[green]Stopped.[/green]")

    console.print(f"[dim]Changing type to {new_type}...[/dim]")
    ec2.modify_instance_attribute(InstanceId=inst_id, InstanceType={'Value': new_type})

    console.print("[dim]Starting...[/dim]")
    ec2.start_instances(InstanceIds=[inst_id])
    ec2.get_waiter('instance_running').wait(InstanceIds=[inst_id])
    console.print("[green]Running.[/green]")

    # --- SSH connect string ---
    new_info = ec2.describe_instances(InstanceIds=[inst_id])['Reservations'][0]['Instances'][0]
    new_dns = new_info.get('PublicDnsName', '')
    new_ip = new_info.get('PublicIpAddress', '')

    ssh_user = 'ec2-user'
    if ami_id:
        try:
            ami = ec2.describe_images(ImageIds=[ami_id])['Images'][0]
            ssh_user = _ssh_user_for_ami(f"{ami.get('Name','')} {ami.get('Description','')}")
        except (ClientError, IndexError):
            pass

    key_path = _find_key_path(key_name) if key_name else "<path-to-your-ssh-key>"
    host = new_dns or new_ip

    console.rule("[bold green]Resize complete[/bold green]")
    console.print(f"  Instance:   {inst_id}")
    console.print(f"  New type:   [cyan]{new_type}[/cyan]")
    console.print(f"  Public IP:  {new_ip or '(none)'}")
    console.print(f"  Public DNS: {new_dns or '(none)'}")
    if host:
        console.print("\n[bold]SSH connect:[/bold]")
        console.print(f"  [cyan]ssh -i {key_path} {ssh_user}@{host}[/cyan]")
    else:
        console.print("\n[yellow]No public address (instance may be in a private subnet).[/yellow]")
    console.rule()


# ---------- action: monthly spend breakdown ----------

def _months_ago(n: int) -> date:
    """First-of-month date n months before the current month."""
    d = date.today().replace(day=1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
    return d


def action_cost_breakdown(region: str) -> None:
    # Cost Explorer is a global service with its API endpoint only in
    # us-east-1, regardless of the user-selected region (which doesn't apply
    # to cost data — costs are account-wide). Each get_cost_and_usage call
    # COSTS $0.01 on the user's bill, so be mindful: don't loop these,
    # don't run this action on a cron without thinking about it.
    ce = boto3.client('ce', region_name='us-east-1')
    today = date.today()

    console.print("\n[dim]Fetching cost history "
                  "(Cost Explorer: $0.01 per API request, ~24hr data lag)...[/dim]")
    try:
        monthly = ce.get_cost_and_usage(
            TimePeriod={'Start': _months_ago(5).isoformat(), 'End': today.isoformat()},
            Granularity='MONTHLY',
            Metrics=['UnblendedCost'],
        )
    except ClientError as e:
        msg = str(e)
        if 'not enabled' in msg.lower() or 'opt' in msg.lower():
            console.print("[red]Cost Explorer isn't enabled on this account.[/red]")
            console.print("Enable: https://console.aws.amazon.com/cost-management/home#/cost-explorer")
            console.print("[dim](Free to enable; only the API itself costs $0.01/request.)[/dim]")
            return
        raise

    tbl = Table(title="Monthly total (last 6 months, UnblendedCost)")
    tbl.add_column("Month", style="cyan")
    tbl.add_column("USD", justify="right", style="green")
    for r in monthly['ResultsByTime']:
        tbl.add_row(r['TimePeriod']['Start'][:7],
                    f"${float(r['Total']['UnblendedCost']['Amount']):,.2f}")
    console.print(tbl)

    # Current month, grouped by service. Skip if it's the 1st (start == end).
    month_start = today.replace(day=1)
    if today == month_start:
        console.print("[dim]It's the 1st of the month — no current-month service breakdown yet.[/dim]")
        return
    by_service = ce.get_cost_and_usage(
        TimePeriod={'Start': month_start.isoformat(), 'End': today.isoformat()},
        Granularity='MONTHLY',
        Metrics=['UnblendedCost'],
        GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}],
    )
    rows = []
    for r in by_service['ResultsByTime']:
        for g in r['Groups']:
            rows.append((g['Keys'][0], float(g['Metrics']['UnblendedCost']['Amount'])))
    rows.sort(key=lambda x: x[1], reverse=True)

    tbl = Table(title=f"Current month ({month_start.isoformat()} -> {today.isoformat()}) by service")
    tbl.add_column("Service", style="cyan")
    tbl.add_column("USD", justify="right", style="green")
    total = 0.0
    for svc, cost in rows:
        total += cost
        if cost < 0.01:
            continue
        tbl.add_row(svc, f"${cost:,.2f}")
    tbl.add_row("[bold]Total[/bold]", f"[bold]${total:,.2f}[/bold]")
    console.print(tbl)


# ---------- main menu ----------

# Add new actions here. Each entry is (menu_label, function). The function
# must accept `region: str` and return None. See README.md for the full
# conventions. Order in this list is the order shown in the menu (item 1,
# item 2, etc.); item 0 is always "Quit" (handled in main()).
ACTIONS = [
    ("Show monthly spend breakdown", action_cost_breakdown),
    ("List EC2 instances",           action_list_instances),
    ("Resize an EC2 instance",       action_resize_instance),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal AWS tool")
    parser.add_argument('--region', default='us-east-1', help='AWS region (default: us-east-1)')
    args = parser.parse_args()

    while True:
        console.print(f"\n[bold cyan]aws-tool[/bold cyan]  [dim](region: {args.region})[/dim]")
        for i, (label, _) in enumerate(ACTIONS, 1):
            console.print(f"  [bold]{i})[/bold] {label}")
        console.print("  [bold]0)[/bold] [dim]Quit[/dim]")
        choices = [str(i) for i in range(0, len(ACTIONS) + 1)]
        choice = IntPrompt.ask("Pick", choices=choices, show_choices=False)
        if choice == 0:
            console.print("Bye.")
            return
        # Catch errors per-action so the user returns to the menu instead of
        # the tool crashing. KeyboardInterrupt is handled here so Ctrl-C
        # during an action aborts that action only, not the whole session.
        # The outermost try/except in __main__ catches Ctrl-C at the menu
        # prompt itself, which exits cleanly.
        try:
            ACTIONS[choice - 1][1](args.region)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted — back to menu.[/yellow]")
        except ClientError as e:
            console.print(f"\n[red]AWS error:[/red] {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\nBye.")
        sys.exit(0)
