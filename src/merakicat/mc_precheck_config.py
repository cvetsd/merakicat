"""Standalone pre-flight check for a Catalyst config before a Merakicat run.

Meraki action batches are atomic: one action Dashboard rejects discards every
other action in the same batch. That makes a single bad port silently cost you
the whole switch, so it is worth catching the known-invalid combinations in the
source config before you push anything.

Usage:
    python mc_precheck_config.py <config.cfg> [<config.cfg> ...]

Exit status is 0 when nothing was found, 1 when something needs a look.
This script only reads files - it never contacts Dashboard.
"""
import re
import sys


def parse_interfaces(text):
    """Return [(interface_name, [child lines])] for every interface block."""
    interfaces = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        match = re.match(r"^interface\s+(\S+)", line)
        if match:
            current = (match.group(1), [])
            interfaces.append(current)
        elif current is not None:
            if line.startswith(" ") and line.strip():
                current[1].append(line.strip())
            elif line.strip() in ("!", ""):
                current = None
    return interfaces


def check_interface(name, body):
    """Return a list of (severity, message) for one interface block."""
    found = []
    joined = "\n".join(body)

    mode = re.search(r"^switchport mode (\S+)", joined, re.M)
    mode = mode.group(1) if mode else None
    voice = re.search(r"^switchport voice vlan (\S+)", joined, re.M)
    native = re.search(r"^switchport trunk native vlan (\S+)", joined, re.M)
    access = re.search(r"^switchport access vlan (\S+)", joined, re.M)
    shut = any(line == "shutdown" for line in body)

    if mode == "trunk" and voice:
        found.append((
            "BLOCKER",
            f"trunk port with 'switchport voice vlan {voice.group(1)}' - Meraki "
            "allows a voice VLAN on access ports only, and will reject this. "
            "Because action batches are atomic, this one port can discard the "
            "entire switch's config."
            + (" (Port is shutdown, so the setting is probably stale.)"
               if shut else "")
        ))

    if mode == "trunk" and native and access:
        found.append((
            "WARNING",
            f"trunk port has both 'access vlan {access.group(1)}' and 'native "
            f"vlan {native.group(1)}'. Meraki has one VLAN field per port; for "
            f"a trunk it means the native VLAN, so {native.group(1)} is what "
            "should be pushed."
        ))

    if mode == "access" and re.search(r"^switchport trunk allowed vlan", joined, re.M):
        found.append((
            "WARNING",
            "access port carries 'switchport trunk allowed vlan' - Meraki "
            "accepts allowedVlans on trunk ports only."
        ))

    return found


def check_svis(text):
    """SVIs with no IP address cannot be created as Meraki L3 interfaces."""
    found = []
    for name, body in parse_interfaces(text):
        if not name.startswith("Vlan"):
            continue
        if not any(line.startswith("ip address ") for line in body):
            found.append((
                "INFO",
                f"{name}: no IP address, so it cannot become a Meraki L3 "
                "interface. Expect it to be reported as skipped."
            ))
    return found


def check_file(path):
    with open(path, encoding="utf-8", errors="ignore") as handle:
        text = handle.read()

    findings = []
    for name, body in parse_interfaces(text):
        for severity, message in check_interface(name, body):
            findings.append((severity, name, message))
    for severity, message in check_svis(text):
        findings.append((severity, "", message))

    print(f"\n=== {path}")
    if not findings:
        print("  Nothing found. No known batch-breaking combinations.")
        return 0

    order = {"BLOCKER": 0, "WARNING": 1, "INFO": 2}
    blockers = 0
    for severity, name, message in sorted(findings, key=lambda f: order[f[0]]):
        label = f"{name}: " if name else ""
        print(f"  [{severity}] {label}{message}")
        if severity == "BLOCKER":
            blockers += 1

    if blockers:
        print(f"\n  {blockers} blocker(s). Fix these on the switch, or expect "
              "the push to apply nothing at all.")
    return 1


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    status = 0
    for path in argv[1:]:
        try:
            status |= check_file(path)
        except OSError as exc:
            print(f"\n=== {path}\n  Could not read it: {exc}")
            status = 2
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
