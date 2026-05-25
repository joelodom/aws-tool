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
import time
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


def pick_number(prompt: str, count: int, allow_cancel: bool = True) -> int | None:
    """Prompt for a number 1..count (or 0 for back). Returns 0-based index or None.

    Use this when the choices are already shown to the user in a rich Table
    (so we don't want pick_from_menu's duplicate numbered list)."""
    if allow_cancel:
        console.print("   [bold]0)[/bold] [dim]Back[/dim]")
    valid = [str(i) for i in range(0 if allow_cancel else 1, count + 1)]
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


def _describe_types_batch(ec2, type_names) -> dict:
    """describe_instance_types caps at 100 InstanceTypes per call AND has a
    small default page size — so we batch in 100-name chunks and paginate
    each batch to make sure we get every result back."""
    result = {}
    names = list(type_names)
    paginator = ec2.get_paginator('describe_instance_types')
    for i in range(0, len(names), 100):
        for page in paginator.paginate(InstanceTypes=names[i:i + 100]):
            for t in page['InstanceTypes']:
                result[t['InstanceType']] = t
    return result


def _wait_for_state(ec2, inst_id: str, target_state: str, action_label: str,
                    poll_interval: int = 5) -> None:
    """Poll DescribeInstances every poll_interval seconds, showing live state
    and elapsed time. Replaces the silent boto3 waiter so the user sees the
    instance walking through 'stopping' -> 'stopped' (or 'pending' -> 'running')
    instead of staring at a frozen line for ~60s."""
    start = time.monotonic()
    state = "unknown"
    with console.status(f"[cyan]{action_label}[/cyan] {inst_id}...", spinner="dots") as status:
        while True:
            info = ec2.describe_instances(InstanceIds=[inst_id])['Reservations'][0]['Instances'][0]
            state = info['State']['Name']
            elapsed = int(time.monotonic() - start)
            status.update(
                f"[cyan]{action_label}[/cyan] {inst_id}  "
                f"state=[yellow]{state}[/yellow]  elapsed={elapsed}s"
            )
            if state == target_state:
                break
            time.sleep(poll_interval)
    console.print(
        f"[green]✓[/green] {inst_id} is now [green]{state}[/green] "
        f"[dim]({int(time.monotonic() - start)}s)[/dim]"
    )


def action_resize_instance(region: str) -> None:
    ec2 = boto3.client('ec2', region_name=region)
    # Pricing is a global service with regional endpoint only in us-east-1
    # (and ap-south-1). Always use us-east-1 regardless of user's region.
    pricing = boto3.client('pricing', region_name='us-east-1')
    location = REGION_TO_LOCATION.get(region)

    console.print(f"\n[dim]Fetching EC2 instances in {region}...[/dim]")
    instances = list_instances(ec2)
    if not instances:
        console.print(f"[yellow]No EC2 instances in {region}.[/yellow]")
        return

    # Each menu level is a loop. "Back" (0) breaks to the outer loop and
    # re-displays it; invalid picks `continue` so the user re-picks at the
    # same level. Back from the outermost (instance) loop returns from
    # the action entirely.

    while True:  # --- L1: pick instance ---
        inst_table = Table(title=f"EC2 instances in {region}")
        inst_table.add_column("#", justify="right")
        inst_table.add_column("Name", style="cyan")
        inst_table.add_column("ID")
        inst_table.add_column("Type")
        inst_table.add_column("State")
        for i, inst in enumerate(instances, 1):
            inst_table.add_row(str(i), name_of(inst) or "(no name)",
                               inst['InstanceId'], inst['InstanceType'],
                               style_state(inst['State']['Name']))
        console.print(inst_table)

        pick = pick_number("Pick an instance", len(instances))
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
            continue

        cur = ec2.describe_instance_types(InstanceTypes=[current_type])['InstanceTypes'][0]
        console.print(
            f"\n[bold]Current:[/bold] [cyan]{current_type}[/cyan]  "
            f"vCPU={cur['VCpuInfo']['DefaultVCpus']}  "
            f"mem={cur['MemoryInfo']['SizeInMiB']/1024:.1f} GiB  "
            f"net={cur['NetworkInfo']['NetworkPerformance']}  "
            f"state={style_state(current_state)}  arch={arch}"
        )

        while True:  # --- L2: pick category ---
            console.print("\n[bold]Instance type categories[/bold]")
            cat_pick = pick_from_menu("Pick a category", [c[0] for c in CATEGORIES])
            if cat_pick is None:
                break  # back to instance loop
            pattern = re.compile(CATEGORIES[cat_pick][1])

            console.print(f"[dim]Fetching available types in {region}...[/dim]")
            paginator = ec2.get_paginator('describe_instance_type_offerings')
            available = set()
            for page in paginator.paginate(LocationType='region'):
                for o in page['InstanceTypeOfferings']:
                    if pattern.match(o['InstanceType']):
                        available.add(o['InstanceType'])
            if not available:
                console.print(f"[yellow]No types in this category for {region}.[/yellow]")
                continue
            families = sorted({t.split('.')[0] for t in available})

            console.print(f"[dim]Fetching details for {len(available)} types...[/dim]")
            type_details = _describe_types_batch(ec2, available)

            family_info: dict[str, dict] = {}
            for fam in families:
                fam_types = [type_details[t] for t in available
                             if t.split('.')[0] == fam and t in type_details]
                if not fam_types:
                    continue
                archs: set[str] = set()
                vcpus: list[int] = []
                rams: list[float] = []
                for t in fam_types:
                    archs.update(t['ProcessorInfo']['SupportedArchitectures'])
                    vcpus.append(t['VCpuInfo']['DefaultVCpus'])
                    rams.append(t['MemoryInfo']['SizeInMiB'] / 1024)
                family_info[fam] = {
                    'archs': sorted(archs),
                    'vcpu_min': min(vcpus), 'vcpu_max': max(vcpus),
                    'ram_min': min(rams), 'ram_max': max(rams),
                    'n_sizes': len(fam_types),
                    'compatible': arch in archs,
                }

            while True:  # --- L3: pick family ---
                fam_table = Table(title=f"Families in {region}  (current arch: [cyan]{arch}[/cyan])")
                fam_table.add_column("#", justify="right")
                fam_table.add_column("Family", style="cyan")
                fam_table.add_column("Arch")
                fam_table.add_column("vCPU", justify="right")
                fam_table.add_column("RAM (GiB)", justify="right")
                fam_table.add_column("Sizes", justify="right")
                for i, fam in enumerate(families, 1):
                    info = family_info.get(fam)
                    if not info:
                        continue
                    archs_str = ", ".join(info['archs'])
                    vcpu_str = f"{info['vcpu_min']}–{info['vcpu_max']}"
                    ram_str = f"{info['ram_min']:.1f}–{info['ram_max']:.1f}"
                    cells = [str(i), fam, archs_str, vcpu_str, ram_str, str(info['n_sizes'])]
                    if not info['compatible']:
                        cells = [f"[dim strike]{c}[/dim strike]" for c in cells]
                    fam_table.add_row(*cells)
                console.print(fam_table)

                fam_pick = pick_number("Pick a family", len(families))
                if fam_pick is None:
                    break  # back to category loop
                family = families[fam_pick]
                if not family_info.get(family, {}).get('compatible'):
                    console.print(f"[red]{family} has no sizes compatible with {arch}.[/red]")
                    continue

                console.print(f"[dim]Fetching sizes and prices for {family}...[/dim]")
                # describe_instance_types paginates with a small default page
                # size — without get_paginator, we only see the first few sizes
                # of any family. (Per CLAUDE.md: always paginate listing APIs.)
                sz_paginator = ec2.get_paginator('describe_instance_types')
                sizes = []
                for page in sz_paginator.paginate(
                    Filters=[{'Name': 'instance-type', 'Values': [f'{family}.*']}]
                ):
                    sizes.extend(page['InstanceTypes'])
                sizes.sort(key=lambda t: (t['VCpuInfo']['DefaultVCpus'], t['MemoryInfo']['SizeInMiB']))
                if not sizes:
                    console.print(f"[yellow]No {family} sizes in {region}.[/yellow]")
                    continue

                def _compat(s) -> bool:
                    return arch in s['ProcessorInfo']['SupportedArchitectures']

                types_in_order = [s['InstanceType'] for s in sizes]
                prices: dict[str, str | None] = {t: None for t in types_in_order}
                # Parallelize price fetches: each Pricing API call is 200-500ms;
                # a family with 15-20 sizes serial = 5-10 seconds of waiting.
                # boto3 clients are thread-safe for read operations like
                # get_products, so sharing one client across threads is fine.
                # Skip incompatible sizes — the user can't pick them anyway.
                if location:
                    compat_types = [s['InstanceType'] for s in sizes if _compat(s)]
                    def _fetch(t: str) -> str | None:
                        return fetch_price(pricing, t, location)
                    with ThreadPoolExecutor(max_workers=10) as pool:
                        for t, p in zip(compat_types, pool.map(_fetch, compat_types)):
                            prices[t] = p

                # Only show Storage / GPU columns when at least one size in the
                # family has the data — otherwise the column is a wall of "—".
                any_store = any(s.get('InstanceStorageSupported') for s in sizes)
                any_gpu = any('GpuInfo' in s for s in sizes)

                while True:  # --- L4: pick size ---
                    sz_table = Table(title=f"{family}.* (Linux on-demand, {region})")
                    sz_table.add_column("#", justify="right")
                    sz_table.add_column("Type", style="cyan")
                    sz_table.add_column("vCPU", justify="right")
                    sz_table.add_column("Cores", justify="right")
                    sz_table.add_column("RAM (GiB)", justify="right")
                    sz_table.add_column("GHz", justify="right")
                    sz_table.add_column("Network")
                    if any_store:
                        sz_table.add_column("Storage")
                    if any_gpu:
                        sz_table.add_column("GPU")
                    sz_table.add_column("USD/hr", justify="right", style="green")
                    for i, s in enumerate(sizes, 1):
                        t = s['InstanceType']
                        compat = _compat(s)
                        marker = " [bold yellow](current)[/bold yellow]" if t == current_type else ""
                        p = prices.get(t)
                        hr_str = f"${float(p):.4f}" if p else ("—" if not compat else "?")

                        vcpu = s['VCpuInfo']
                        cores = vcpu.get('DefaultCores')
                        ghz = s['ProcessorInfo'].get('SustainedClockSpeedInGhz')

                        row = [
                            str(i),
                            f"{t}{marker}",
                            str(vcpu['DefaultVCpus']),
                            str(cores) if cores else "?",
                            f"{s['MemoryInfo']['SizeInMiB']/1024:.1f}",
                            f"{ghz:.1f}" if ghz else "—",
                            s['NetworkInfo']['NetworkPerformance'],
                        ]
                        if any_store:
                            if s.get('InstanceStorageSupported'):
                                store = s.get('InstanceStorageInfo', {})
                                total = store.get('TotalSizeInGB', '?')
                                nvme = " NVMe" if store.get('NvmeSupport') in ('required', 'supported') else ""
                                row.append(f"{total} GB{nvme}")
                            else:
                                row.append("—")
                        if any_gpu:
                            gpu = s.get('GpuInfo')
                            if gpu and gpu.get('Gpus'):
                                gpus = gpu['Gpus']
                                total = sum(g.get('Count', 0) for g in gpus)
                                first = gpus[0]
                                name = f"{first.get('Manufacturer', '')} {first.get('Name', '')}".strip()
                                row.append(f"{total}x {name}")
                            else:
                                row.append("—")
                        row.append(hr_str)
                        if not compat:
                            row = [f"[dim strike]{c}[/dim strike]" for c in row]
                        sz_table.add_row(*row)
                    console.print(sz_table)

                    size_pick = pick_number("Pick new size", len(sizes))
                    if size_pick is None:
                        break  # back to family loop
                    new_type = types_in_order[size_pick]
                    if not _compat(sizes[size_pick]):
                        console.print(f"[red]{new_type} is not compatible with {arch}.[/red]")
                        continue
                    if new_type == current_type:
                        console.print("[yellow]That's the current type. Nothing to do.[/yellow]")
                        continue

                    console.print(
                        f"\n[bold]Resize plan:[/bold] {inst_id} ({name_of(sel) or 'no name'})  "
                        f"[cyan]{current_type}[/cyan] -> [cyan]{new_type}[/cyan]"
                    )
                    console.print("Steps: stop -> modify type -> start. Public IP will change unless an Elastic IP is attached.")
                    if not Confirm.ask("Proceed?", default=False):
                        console.print("Cancelled.")
                        continue  # back to size pick

                    # --- execute ---
                    # _wait_for_state polls DescribeInstances every 5s and
                    # shows live state + elapsed time so the user can see the
                    # instance walk through stopping->stopped / pending->running
                    # rather than staring at a frozen line.
                    if current_state == 'running':
                        ec2.stop_instances(InstanceIds=[inst_id])
                        _wait_for_state(ec2, inst_id, 'stopped', 'Stopping')

                    console.print(f"[dim]Changing type to[/dim] [cyan]{new_type}[/cyan]...")
                    ec2.modify_instance_attribute(InstanceId=inst_id, InstanceType={'Value': new_type})
                    console.print("[green]✓[/green] Instance type changed.")

                    ec2.start_instances(InstanceIds=[inst_id])
                    _wait_for_state(ec2, inst_id, 'running', 'Starting')

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
                        # Print the ssh command flush-left so triple-click /
                        # copy-paste grabs exactly the command with no
                        # leading whitespace.
                        console.print("\n[bold]SSH connect (copy/paste):[/bold]")
                        console.print(f"[cyan]ssh -i {key_path} {ssh_user}@{host}[/cyan]")
                    else:
                        console.print("\n[yellow]No public address (instance may be in a private subnet).[/yellow]")
                    console.rule()
                    return


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
