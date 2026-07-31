"""Offline regression checks for the translate-path fixes.

There is no test suite in this repo, which is a large part of why a silently
broken config push went unnoticed. These checks need no Dashboard access and
no switch: they run the pure helpers and the exec'd encyclopedia snippets
against the real captured port dictionaries in TempFiles/.

Usage (from src/merakicat):
    python mc_verify_fixes.py

Exit status 0 means every assertion held.
"""
import json
import os
import sys
import warnings
from collections import defaultdict

warnings.simplefilter("ignore", SyntaxWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEMP = os.path.join("..", "..", "TempFiles")
MDF_PD = os.path.join(TEMP, "Clairmont_MDF.pd")
IDF_PD = os.path.join(TEMP, "Clairmont_IDF_C137.pd")
WWTP_CFG = os.path.join(TEMP, "WWTP-Reuse-SW1.cfg")

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")
    else:
        shown = repr(got)
        print(f"  ok   {label} = {shown[:90]}")


def section(title):
    print(f"\n--- {title}")


def ports(pd_path):
    """Yield (name, settings, a copy of the args as originally sent)."""
    for name, settings in json.load(open(pd_path)).items():
        if "Vlan" in name or "Port-channel" in name:
            continue
        yield name, settings, dict(settings["meraki_args"][2])


class StubSwitch:
    def __init__(self):
        self.svis = []
        self.routes = []

    def createNetworkSwitchStackRoutingInterface(self, networkId, switchStackId,
                                                 name, **kwargs):
        self.svis.append(name)

    def createNetworkSwitchStackRoutingStaticRoute(self, networkId, stackId,
                                                   subnet, gw):
        self.routes.append((subnet, gw))


class StubDash:
    def __init__(self):
        self.switch = StubSwitch()


def run_snippet(snippet, extra):
    dash = StubDash()
    ctx = {"dashboard": dash, "sw_list": ["Q1"], "debug": False,
           "unified_os": False, "meraki_api_key": "x"}
    ctx.update(extra)
    out = {}
    exec(snippet, ctx, out)
    return dash, out


def main():
    from mc_pedia2 import mc_pedia
    from mc_translate import (Evaluate, is_physical_port_type,
                              model_port_count, reconcile_port_vlan_args,
                              reconcile_ports_against_batches,
                              summarize_parsed_ports,
                              validate_port_action_args)
    import mc_meraki_dry_run
    import mc_translate

    # ---------------------------------------------------------- vlan / voice
    section("The Clairmont_MDF blocker: voiceVlan on trunk ports")
    pre_mdf = [p for n, s, a in ports(MDF_PD)
               for p in validate_port_action_args(a, n)]
    pre_idf = [p for n, s, a in ports(IDF_PD)
               for p in validate_port_action_args(a, n)]
    check("payloads as originally sent: MDF problems", len(pre_mdf), 4)
    check("payloads as originally sent: IDF problems", len(pre_idf), 0)

    fixed = {}
    for name, settings, args in ports(MDF_PD):
        reconcile_port_vlan_args(args, settings, name)
        fixed[name] = args
    for port in ("GigabitEthernet2/0/1", "GigabitEthernet2/0/2",
                 "GigabitEthernet2/0/3", "GigabitEthernet2/0/4"):
        check(f"{port} uses the native VLAN", fixed[port].get("vlan"), "999")
        check(f"{port} voiceVlan removed", "voiceVlan" in fixed[port], False)
    check("trunk with no access VLAN is untouched (Gi1/0/47)",
          fixed["GigabitEthernet1/0/47"]["vlan"], "67")
    check("access port keeps its voice VLAN (Gi1/0/23)",
          fixed["GigabitEthernet1/0/23"].get("voiceVlan"), "177")
    check("MDF problems after the fix",
          len([p for n, a in fixed.items()
               for p in validate_port_action_args(a, n)]), 0)

    section("IDF is the known-good control: the fix must be a no-op")
    changed = []
    for name, settings, args in ports(IDF_PD):
        before = dict(args)
        reconcile_port_vlan_args(args, settings, name)
        if args != before:
            changed.append(name)
    check("IDF ports altered by the fix", changed, [])

    # ------------------------------------------------------------- SVI gate
    section("SVI creation is gated on 'ip routing'")
    svis = {k: v for k, v in json.load(open(MDF_PD)).items() if "Vlan" in k}
    snippet = mc_pedia["port"]["l3_interface"]["meraki"]["post_ports_process"]

    dash, out = run_snippet(snippet, {
        "port_dict": svis,
        "switch_dict": {"switchStackId": "s1", "ip_routing": []}})
    check("L2 switch: no SVI API calls", dash.switch.svis, [])
    check("L2 switch: both SVIs reported", sorted(out["unconf_ports"]),
          ["Vlan1", "Vlan67"])

    dash, out = run_snippet(snippet, {
        "port_dict": svis,
        "switch_dict": {"switchStackId": "s1", "ip_routing": ["<obj>"]}})
    check("routed switch: Vlan67 created", dash.switch.svis, ["Vlan67"])
    check("routed switch: IP-less Vlan1 skipped", out["unconf_ports"], ["Vlan1"])

    no_gw = {"Vlan10": {"meraki_args": ["n", "s", "Vlan10", "10",
                                        {"interfaceIp": "10.0.0.1",
                                         "subnet": "10.0.0.0/24"}]}}
    try:
        dash, out = run_snippet(snippet, {
            "port_dict": no_gw,
            "switch_dict": {"switchStackId": "s1", "ip_routing": ["<obj>"]}})
        check("no default-gateway SVI does not raise NameError",
              out["conf_ports"], ["Vlan10"])
    except NameError as exc:
        check("no default-gateway SVI does not raise NameError",
              f"NameError: {exc}", "no exception")

    section("Static routes are gated the same way")
    route_snip = mc_pedia["switch"]["static_routing"]["meraki"]["post_ports_process"]
    routes = [{"subnet": "10.5.0.0/16", "gw": "10.67.23.1"},
              {"subnet": "0.0.0.0/0", "gw": "10.67.23.1"}]
    for label, ip_routing, want in (("L2 switch", [], []),
                                    ("routed switch", ["<obj>"],
                                     [("10.5.0.0/16", "10.67.23.1")])):
        dash, _ = run_snippet(route_snip, {
            "switch_dict": {"static_routing": routes, "switchStackId": "s1",
                            "networkId": "n1", "ip_routing": ip_routing}})
        check(f"{label}: static route API calls", dash.switch.routes, want)

    # ------------------------------------------------------- SDK signatures
    section("The stack SVI call binds on every supported meraki SDK")
    seen = {}

    class Old:  # meraki 1.46.0
        def createNetworkSwitchStackRoutingInterface(self, networkId,
                                                     switchStackId, name,
                                                     vlanId, **kw):
            seen["old"] = (networkId, switchStackId, name, vlanId, kw)

    class New:  # meraki 2.0.3 / 3.x / 4.x
        def createNetworkSwitchStackRoutingInterface(self, networkId,
                                                     switchStackId, name, **kw):
            seen["new"] = (networkId, switchStackId, name,
                           kw.pop("vlanId", None), kw)

    ma = ["net", "stack", "Vlan67", "67", {"interfaceIp": "10.67.23.2"}]
    for sdk in (Old(), New()):
        sdk.createNetworkSwitchStackRoutingInterface(
            ma[0], ma[1], name=ma[2], vlanId=ma[3], **ma[4])
    check("both SDK shapes receive identical arguments", seen["old"], seen["new"])

    # ------------------------------------------------- batch reconciliation
    section("Batch outcomes drive the report, not intent")
    mc_translate.BATCH_POLL_INTERVAL_SECONDS = 0
    mc_translate.BATCH_POLL_TIMEOUT_SECONDS = 0.25
    dry_was = mc_meraki_dry_run.MERAKI_DRY_RUN
    mc_meraki_dry_run.MERAKI_DRY_RUN = False

    DONE = {"completed": True, "failed": False, "errors": []}
    BAD = {"completed": False, "failed": True,
           "errors": ["voiceVlan: Only applicable to access ports"]}

    class Orgs:
        def __init__(self, script):
            self.script = script
            self.reads = defaultdict(int)

        def getOrganizationActionBatch(self, org, bid):
            self.reads[bid] += 1
            pending, final = self.script[bid]
            if self.reads[bid] <= pending:
                return {"status": {"completed": False, "failed": False}}
            return {"status": final}

    class Dash:
        def __init__(self, script):
            self.organizations = Orgs(script)

    def fixture():
        conf, owners = defaultdict(list), []
        for p in range(1, 101):
            conf[0].append(str(p))
            owners.append((0, str(p)))
        for p in range(1, 21):
            conf[1].append(str(p))
            owners.append((1, str(p)))
        return conf, defaultdict(list), owners

    conf, unconf, owners = fixture()
    reconcile_ports_against_batches(Dash({"b0": (2, DONE), "b1": (1, DONE)}),
                                    "org", ["b0", "b1"], owners, conf, unconf, 100)
    check("a batch that is merely pending is not called failed",
          (len(conf[0]), len(conf[1]), len(unconf[0])), (100, 20, 0))

    conf, unconf, owners = fixture()
    reconcile_ports_against_batches(Dash({"b0": (1, BAD), "b1": (0, DONE)}),
                                    "org", ["b0", "b1"], owners, conf, unconf, 100)
    check("a failed batch demotes exactly its own ports",
          (conf[0], len(unconf[0]), len(conf[1])), ([], 100, 20))

    conf, unconf, owners = fixture()
    reconcile_ports_against_batches(Dash({"b0": (float("inf"), DONE)}),
                                    "org", ["b0"], owners[:100], conf, unconf, 100)
    check("an unconfirmed batch is not reported as success",
          (conf[0], len(unconf[0])), ([], 100))

    conf, unconf, owners = fixture()
    mc_meraki_dry_run.MERAKI_DRY_RUN = True
    d = Dash({"b0": (0, BAD)})
    reconcile_ports_against_batches(d, "org", ["b0"], owners, conf, unconf, 100)
    check("dry run does not poll", dict(d.organizations.reads), {})
    mc_meraki_dry_run.MERAKI_DRY_RUN = dry_was

    # -------------------------------------------------------- parsing parity
    section("Encyclopedia edits did not change config parsing")
    ignore = {"meraki_args", "translation_notes"}
    for name, pd_path in (("Clairmont_MDF", MDF_PD),
                          ("Clairmont_IDF_C137", IDF_PD)):
        stored = json.load(open(pd_path))
        cfg = os.path.join(TEMP, f"{name}.cfg")
        _, _, port_dict, switch_dict = Evaluate(cfg, [""] * 9, False)
        check(f"{name}: same ports parsed",
              sorted(port_dict) == sorted(stored), True)
        drift = [(p, k) for p, parsed in port_dict.items()
                 for k, v in parsed.items()
                 if k not in ignore and stored.get(p, {}).get(k) != v]
        check(f"{name}: parsed values match what was sent", drift, [])
        check(f"{name}: detected as Layer 2 (no 'ip routing')",
              switch_dict.get("ip_routing"), [])

    # --------------------------------------------------- port type vocabulary
    section("Every IOS XE front-panel port type is recognized")
    # The old hardcoded list in split_down_up_link named eight types and
    # omitted TwoGigabitEthernet, so a C9300-48UXM's 36 mGig downlinks were
    # dropped into Other_list (which nothing reads) and the port count check
    # aborted the run with "has 48 ports, not 12!". Speed names keep being
    # added, so assert the suffix rule against the whole vocabulary.
    for intf_type in ("FastEthernet", "GigabitEthernet", "TwoGigabitEthernet",
                      "FiveGigabitEthernet", "TenGigabitEthernet",
                      "TwentyFiveGigE", "FortyGigabitEthernet",
                      "HundredGigabitEthernet", "HundredGigE",
                      "TwoHundredGigabitEthernet",
                      "FourHundredGigabitEthernet"):
        check(f"{intf_type} is a physical port", is_physical_port_type(intf_type),
              True)
    for intf_type in ("Vlan", "Port-channel", "Tunnel", "Loopback",
                      "Bluetooth"):
        check(f"{intf_type} is not a physical port",
              is_physical_port_type(intf_type), False)

    # ------------------------------------------------------ WWTP-Reuse-SW1
    if not os.path.isfile(WWTP_CFG):
        print(f"\n--- SKIPPED: {WWTP_CFG} not present, so the C9300-48UXM "
              "mGig regression is unverified this run.")
    else:
        section("C9300-48UXM mGig downlinks reach the port count check")
        Intf_list, Other_list, port_dict, switch_dict = Evaluate(
            WWTP_CFG, [""] * 9, True)
        summary = summarize_parsed_ports(port_dict, Intf_list)
        check("nothing is dropped into the unread Other_list", Other_list, [])
        check("switch 1 downlinks parsed", len(summary[1]["downlinks"]), 48)
        check("counted by interface type", dict(summary[1]["types"]),
              {"TwoGigabitEthernet": 36, "TenGigabitEthernet": 12})
        check("model port count agrees, so the run proceeds",
              model_port_count("C9300-48UXM"), 48)
        # Uplink module ports need `show inventory` over SSH (mc_get_nms), so a
        # file-based translate cannot place them and must leave them out rather
        # than guess a Meraki port number.
        check("module ports stay out of Intf_list",
              [p for p in Intf_list if "/1/" in p], [])
        check("both SVIs parsed", sorted(p for p in Intf_list
                                        if p.startswith("Vlan")),
              ["Vlan1", "Vlan9"])
        check("detected as Layer 2 (no 'ip routing')",
              switch_dict.get("ip_routing"), [])
        check("voice VLAN on access ports survives",
              port_dict["TwoGigabitEthernet1/0/1"].get("voiceVlan"), "90")
        check("the one trunk downlink keeps its native VLAN",
              (port_dict["TwoGigabitEthernet1/0/2"].get("type"),
               port_dict["TwoGigabitEthernet1/0/2"].get("nativeVlan")),
              ("trunk", "9"))

        section("An uplink module model we don't know must not abort the run")
        # nm_dict covers 18 modules and no C9200 ones at all, and GetNmList
        # feeds it whatever string the switch reports. Both lookups used to be
        # unguarded, so an unlisted model raised a bare KeyError after the
        # config had already been pulled over SSH.
        try:
            unknown = Evaluate(WWTP_CFG, ["C9200-NM-4X"] + [""] * 8, True)
            check("unknown module: downlinks still parsed",
                  len(summarize_parsed_ports(unknown[2], unknown[0])[1]
                      ["downlinks"]), 48)
            check("unknown module: its ports are left out",
                  [p for p in unknown[0] if "/1/" in p], [])
        except KeyError as exc:
            check("unknown module does not raise KeyError",
                  f"KeyError: {exc}", "no exception")
        known = Evaluate(WWTP_CFG, ["C9300-NM-4G"] + [""] * 8, True)
        check("known module: its configured ports are translated",
              [p for p in known[0] if "/1/" in p],
              ["GigabitEthernet1/1/2", "GigabitEthernet1/1/3",
               "GigabitEthernet1/1/4"])

    section("An interface with no Meraki port number is never pushed")
    # If args ever gains no entry for an Intf_list index, every later args[y]
    # belongs to a different interface - so the fallback keeps the list aligned
    # and relies on the validator to drop the placeholder.
    ok_args = {"type": "trunk"}
    for port_id in ("1", "48", "1_C9300-NM-4G_2"):
        check(f"port id {port_id!r} is accepted",
              validate_port_action_args(ok_args, "Gi1/0/1", port_id), [])
    for port_id in ("GigabitEthernet1/1/2", "", "1_"):
        check(f"port id {port_id!r} is rejected",
              len(validate_port_action_args(ok_args, "Gi1/1/2", port_id)), 1)
    check("port id is only checked when supplied",
          validate_port_action_args(ok_args, "Gi1/0/1"), [])

    # ------------------------------------------------- precheck version gate
    section("The IOS XE version prechecks are reachable")
    from mc_prechecks import prechecks

    # Everything except 'show version' is answered with clean output, so the
    # only issue a run can report is the version one under test.
    # Line offsets matter: prechecks reads index 3 or 6 depending on release,
    # then locates the data rows two lines below the "Switch#" header.
    COMPAT = (
        "\n"
        "Migration compatibility for this switch/stack\n"
        "\n"
        "\n"
        "Switch#  Model        Compatible?  Bootloader  Compatible?\n"
        "-------  -----        -----------  ----------  -----------\n"
        "1        C9300-48UXM  Compatible   17.12.1     Compatible"
    )

    class StubConn:
        def __init__(self, version):
            self.version = version

        def send_command(self, cmd):
            if cmd == "show version":
                return f"Cisco IOS XE Software\nCisco IOS Software, Version {self.version}\n"
            if cmd == "show ip name-servers":
                return "192.168.9.1"
            if cmd.startswith("show ip int brief"):
                return "Vlan9  192.168.9.243  YES  NVRAM  up  up"
            if cmd.startswith("show ip route"):
                return "S* 0.0.0.0/0 [1/0] via 192.168.9.250"
            if cmd == "show meraki compatibility":
                return COMPAT
            return ""

    # Nested under `if v[0] < 17`, all three of these were unreachable for any
    # 17.x release, so the two versions with documented registration failures
    # were never named - and 16.12.5 passed because its patch was not < 1.
    KNOWN_ISSUE = "There is a known issue registering to Dashboard from IOSXE "
    for version, want in (
        ("16.12.5", ["IOSXE version 16.12.5 is less than 17.10.1"]),
        ("17.9.5", ["IOSXE version 17.9.5 is less than 17.10.1"]),
        ("17.10.1", []),
        ("17.13.1", [KNOWN_ISSUE + "17.13.1"]),
        ("17.15.1", []),
        ("17.15.3", [KNOWN_ISSUE + "17.15.3"]),
        ("17.18.1", []),
    ):
        result = prechecks(StubConn(version))
        check(f"{version}: reported issues", result.issues, want)
    check("17.18.1 is a unified OS release",
          prechecks(StubConn("17.18.1")).unified_os, True)
    check("17.12.4 is not a unified OS release",
          prechecks(StubConn("17.12.4")).unified_os, False)

    print("\n" + ("PASS - all assertions held" if not FAILURES
                  else f"FAIL - {len(FAILURES)}: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    if not os.path.isfile(MDF_PD):
        print(f"Cannot find {MDF_PD}. Run this from src/merakicat, with the "
              "captured Clairmont .pd/.cfg files in TempFiles/.")
        sys.exit(2)
    sys.exit(main())
