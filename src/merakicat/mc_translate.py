import batch_helper
import meraki
import pprint
import re
import sys
import time
from ciscoconfparse2 import CiscoConfParse
from collections import defaultdict
from mc_pedia2 import mc_pedia, nm_dict
import mc_meraki_dry_run

try:
    from mc_user_info import DEBUG, DEBUG_TRANSLATOR
except ImportError:
    DEBUG = DEBUG_TRANSLATOR = False

# Set by merakicat.py --ignore-port-count. The source/target port count check
# in MerakiConfig is an assertion about our own parsing, not about the
# hardware, so a bad parse must not strand someone in the field.
IGNORE_PORT_COUNT = False


def set_ignore_port_count(value: bool) -> None:
    """Let the front end downgrade the port count check to a warning."""
    global IGNORE_PORT_COUNT
    IGNORE_PORT_COUNT = bool(value)


def is_physical_port_type(intf_type: str) -> bool:
    """
    Is this IOS XE interface type a physical switch port?

    Matched by suffix rather than an explicit list because IOS XE names a port
    after its speed, and the speed vocabulary keeps growing: a C9300-48UXM
    calls its 2.5G mGig downlinks TwoGigabitEthernet and a C9300-48UN calls
    its 5G ones FiveGigabitEthernet. Every type this misses is silently
    dropped from translation - it lands in Other_list, which nothing reads -
    and also breaks the port count check in MerakiConfig, so err toward
    matching. AppGigabitEthernet is not a front-panel port, but it is already
    filtered out before we get here.
    :param intf_type: Interface name with digits and slashes stripped
    :return: True for a front-panel port type
    """
    return intf_type.endswith(("Ethernet", "GigE"))


def Evaluate(config_file, nm_list, unified_os):
    """
    This parent function will evaluate a Catalyst switch config file and
    reports on which features that are being used can and cannot be mapped to
    Meraki features.
    :param config_file: The catalyst IOSXE config file to parse through
    :param nm_list: List of Network Modules per switch
    :return: Lists of uplinks, downlinks and other ports as well as a
    :      : dictionary with all ports and their settings, and the hostname
    """

    debug = DEBUG or DEBUG_TRANSLATOR

    port_dict = {}
    switch_dict = {}
    # List of interfaces that are shut
    shut_interfaces = list()
    # Interfaces we could not split into a module/sub-module/port
    unparsed_interfaces = list()
    # Uplink/module ports left out because no uplink module was recognized
    skipped_uplinks = list()

    def read_Cisco_SW():
        """
        This sub-function parses the Catalyst switch config file and report on
        which features that are being used can and cannot be mapped to Meraki
        features.
        :param NONE: Using global variables in the parent function
        :return: Lists of uplinks, downlinks and other ports as well as a
        :      : dictionary with all the ports and their settings, and the
        :      : hostname
        """
        for key, val in mc_pedia["switch"].items():
            switch_dict[key] = ""
        if debug:
            print(f"switch_dict = {switch_dict}")
        # Parsing the Cisco Catalyst configuration
        if debug:
            print("-------- Reading <" + config_file + "> Configuration --------")
        parse = CiscoConfParse(config_file, syntax="ios")
        # Try out our mc_pedia for the switch name
        for key, val in mc_pedia["switch"].items():
            newvals = {}
            exec(val.get("iosxe"), locals(), newvals)
            try:
                switch_dict[key] = newvals[key]
                if debug:
                    print(f"switch_dict['{key}'] = {switch_dict[key]}")
            except KeyError:
                if debug:
                    print(f"KeyError: '{key}' not found in newvals for switch")

        Gig_uplink = list()
        Ten_Gig_uplink = list()
        Twenty_Five_Gig_uplink = list()
        Forty_Gig_uplink = list()
        Hundred_Gig_uplink = list()
        All_interfaces = defaultdict(list)

        # Grab all shutdown interfaces
        intf_for_test = parse.find_parent_objects([r"^interface", r"^\s+shutdown"])
        # Remove the management, Loopback, and AppGig interfaces from
        # the intf_for_test list
        intf_for_test[:] = [
            y
            for y in intf_for_test
            if not (
                y.re_match_typed(r"^interface\s+(\S.*)$") == "GigabitEthernet0/0"
                or y.re_match_typed(r"^interface\s+(\S.*)$").startswith("Loopback")
                or y.re_match_typed(r"^interface\s+(\S.*)$").startswith("AppGig")
            )
        ]
        # Test the remaining interfaces for shutdown and add to shut list
        for intf_obj in intf_for_test:
            shut_interfaces.append(intf_obj.re_match_typed(r"^interface\s+(\S.+?)$"))

        # Select all interfaces
        intf = parse.find_objects(r"^interface")
        if debug:
            print(f"intf = {intf}")
        # Remove the management, Loopback, and AppGig interfaces from
        # the interface list
        intf[:] = [
            x
            for x in intf
            if not (
                x.re_match_typed(r"^interface\s+(\S.*)$") == "GigabitEthernet0/0"
                or x.re_match_typed(r"^interface\s+(\S.*)$").startswith("Loopback")
                or x.re_match_typed(r"^interface\s+(\S.*)$").startswith("AppGig")
            )
        ]

        for intf_obj in intf:
            # Get the interface name
            intf_name = intf_obj.re_match_typed(r"^interface\s+(\S.*)$")
            # Default to include this interface_descriptor
            intf_include = True

            # Only interface name will be used to catogrize different types of
            # interfaces (downlink and uplink)
            only_intf_name = re.sub("\d+|\\/", "", intf_name)  # DON'T EDIT!
            if not only_intf_name == "Vlan":
                if only_intf_name == "Port-channel":
                    Switch_module = "0"
                else:
                    Switch_module = intf_obj.re_match_typed(
                        r"^interface\s\S+?thernet+(\d)"
                    )
                    if Switch_module == "":
                        Switch_module = intf_obj.re_match_typed(
                            r"^interface\s\S+?GigE+(\d)"
                        )
                port, sub_module = check(intf_name)
                if sub_module == "":
                    # check() couldn't find a module/sub-module/port in the
                    # name (Tunnel, Bluetooth, a two-part GigabitEthernet0/1).
                    # Meraki has nothing to map it to, so leave it out rather
                    # than push a port with no number.
                    unparsed_interfaces.append(intf_name)
                    intf_include = False
                elif sub_module == "1":
                    intf_include = False
                    if debug:
                        print(f"Switch_module = {Switch_module}")
                        print(f"nm_list = {nm_list}")
                    if not nm_list[int(Switch_module) - 1] == "":
                        if nm_dict[nm_list[int(Switch_module) - 1]]["supported"]:
                            for regex in nm_dict[nm_list[int(Switch_module) - 1]][
                                "ports"
                            ]:
                                if re.match(regex, intf_name):
                                    # Make sure it has some settings,
                                    # or skip it
                                    if not intf_obj.children == []:
                                        intf_include = True
                                        if debug:
                                            print(
                                                f"I matched {intf_name} "
                                                + f"to {regex}\n"
                                            )
                                        break
                    if not intf_include:
                        # Dropped because no recognized uplink module holds it
                        # or it carries no settings, so it never reaches
                        # port_dict and can't show up in the port count
                        # diagnostics. Remember it for the NOTE below.
                        skipped_uplinks.append((Switch_module or "?", intf_name))
                if intf_include:
                    # If we are including the interfaces, let's set a few
                    # default settings
                    All_interfaces[only_intf_name].append(intf_name)
                    port_dict[intf_name] = {}
                    port_dict[intf_name]["sw_module"] = "1"
                    port_dict[intf_name]["sub_module"] = sub_module
                    port_dict[intf_name]["port"] = port
                    port_dict[intf_name]["mac"] = []
                    port_dict[intf_name]["active"] = "true"

                    if debug:
                        print("Made it to Switch_module test")
                    if Switch_module == "0":
                        port_dict[intf_name]["sw_module"] = "1"
                    if not Switch_module == "" and not Switch_module == "0":
                        port_dict[intf_name]["sw_module"] = Switch_module

                    port_dict[intf_name]["mac"].clear()
                    # Check if the interface in the shutdown list then
                    # mark it as shutdown
                    if intf_name in shut_interfaces:
                        port_dict[intf_name]["active"] = "false"

            else:
                # interface Vlan
                All_interfaces[only_intf_name].append(intf_name)
                port_dict[intf_name] = {}
                port_dict[intf_name]["active"] = "true"
                port_dict[intf_name]["vlan"] = intf_obj.re_match_typed(
                    r"^interface\sVlan(\d+)"
                )
                if debug:
                    print(
                        f"for {intf_name}, vlan is " + f"{port_dict[intf_name]['vlan']}"
                    )

            if intf_include:
                if debug:
                    print("Checking children")
                int_fx = intf_obj.children
                if debug:
                    print(f"Children are: {int_fx}")

                # Capture the configuration of the interface
                port_sec_raw = max_mac = ""
                r = r"\sswitchport\sport-security\smac-address\ssticky\s+(\S.+)"
                for child in int_fx:
                    # Try out our mc_pedia
                    if debug:
                        print(f"child = {child}")
                    for key, val in mc_pedia["port"].items():
                        newvals = {}
                        if debug:
                            print(f"key, val = {key},{val}")
                        if not val["regex"] == "":
                            if not child.re_match_typed(regex=val["regex"]) == "":
                                if not val["iosxe"] == "":
                                    exec(val.get("iosxe"), locals(), newvals)
                                    if debug:
                                        print(f"newvals[{key}] = " + f"{newvals[key]}")
                                    if not newvals[key] == "":
                                        i = intf_name
                                        port_dict[i][key] = newvals[key]
                    try:
                        port_sec_raw = child.re_match_typed(regex=r)
                    except:
                        pass
                    try:
                        regex = r"\sswitchport\sport-security\smaximum\s+(\d)"
                        max_mac = child.re_match_typed(regex=regex)
                    except:
                        pass
                    if not port_sec_raw == "":
                        port_dict[intf_name]["mac"].append(mac_build(port_sec_raw))
                    if not max_mac == "":
                        port_dict[intf_name]["Port_Sec"] = max_mac

        Intf_list, Other_list = split_down_up_link(All_interfaces, Gig_uplink)
        # Nothing downstream reads Other_list or the unparsed interfaces, so
        # say out loud what we are dropping. A silently dropped downlink both
        # goes unconfigured and throws off the port count check in
        # MerakiConfig, which is a confusing way to find out about it.
        dropped = [i for i in Other_list if not i.startswith("Port-channel")]
        dropped += unparsed_interfaces
        if dropped:
            print(
                f"NOTE: {len(dropped)} interface(s) in the config will not be "
                + "translated because they are not front-panel switch ports: "
                + ", ".join(dropped)
            )
        if skipped_uplinks:
            # Per switch rather than by name: on a stack this is a dozen or
            # more ports every run, and what matters when a port count comes
            # up short is how many went where, not which ones.
            per_switch = defaultdict(int)
            for switch_module, _ in skipped_uplinks:
                per_switch[switch_module] += 1
            print(
                f"NOTE: {len(skipped_uplinks)} uplink/module port(s) were not "
                + "translated, having no recognized uplink module or no "
                + "settings ("
                + ", ".join(f"switch {sw}: {n}" for sw, n in sorted(per_switch.items()))
                + ")."
            )
            if debug:
                print(f"  skipped: {', '.join(n for _, n in skipped_uplinks)}")
        if debug:
            print(f"Intf_list = {Intf_list}\n")
            print(f"Other_list = {Other_list}\n")
            print(f"port_dict = {port_dict}\n")
            print(f"switch_dict = {switch_dict}\n")
        return Intf_list, Other_list, port_dict, switch_dict

    def split_down_up_link(interfaces_list, Gig_uplink):
        """
        This sub-function takes a list of interfaces and sorts them into
        uplinks, downlinks and others.
        :param interfaces_list: The dictlist of interfaces categorized by type
        :param Gig_uplink:
        :return: Lists of uplinks, downlinks and other ports
        """
        Intf_list = list()
        Other_list = list()

        # Creating a copy of the interface list to avoid Runtime error
        interfaces_list_copy = interfaces_list.copy()
        if debug:
            print(f"interfaces_list = {interfaces_list}\n")
        for key, value in interfaces_list.items():
            for value in interfaces_list_copy[key]:
                if key == "Vlan" or is_physical_port_type(key):
                    # pass
                    Intf_list.append(value)
                else:
                    Other_list.append(value)
        return Intf_list, Other_list

    # rebuild the mac address to match Meraki format
    def mac_build(my_str, group=2, char=":"):
        """
        This sub-function takes an IOSXE MAC address and reformats it into the
        Meraki format.
        :param my_str: The MAC address from IOSXE
        :param group: How many digits to group between delimiters (2 = :00:)
        :param char: The delimiter to use between digit groupings
        :return: A string containing the translate MAC address
        """
        port_sec = re.findall(re.compile(r"[a-fA-F0-9.]{14}"), my_str)[0]
        new_p = re.sub("\.", "", port_sec)  # DO NOT EDIT THIS FOR FLAKE8!
        my_str = str(new_p)

        last = char.join(my_str[i : i + group] for i in range(0, len(my_str), group))
        return last

    # Extract out the details of the switch module and the port number
    def check(intf):
        """
        This sub-function takes an IOSXE long interface name and splits it
        into a submodule and a port.
        :param intf: An IOSXE long interface name
        :return: String with the submodule number and one with the port number
        """
        if debug:
            print(f"Checking interface {intf}.")
        obj = re.search(r"(?:Ethernet|GigE|channel)(\d+)\/(\d+)\/(\d+)$", intf)
        if obj is not None:
            if debug:
                print(f"obj.group(0) = {obj.group(0)}")
                print(f"obj.group(1) = {obj.group(1)}")
                print(f"obj.group(2) = {obj.group(2)}")
                print(f"obj.group(3) = {obj.group(3)}")
            port = obj.group(3)
            Sub_module = obj.group(2)
        else:
            obj = re.search(r"(?:channel)(\d+)$", intf)
            if obj is not None:
                if debug:
                    print(f"obj.group(0) = {obj.group(0)}")
                    print(f"obj.group(1) = {obj.group(1)}")
                port = obj.group(1)
                Sub_module = 0
            else:
                # Neither module/sub-module/port nor Port-channel: a Tunnel,
                # Bluetooth, or two-part name like GigabitEthernet0/1. Don't
                # guess a port number - anything without a sub-module of "0"
                # is left out of the port count and never configured, which is
                # what we want for a non-front-panel interface.
                if debug:
                    print(f"Couldn't split {intf} into module/port.")
                port = ""
                Sub_module = ""
        return port, Sub_module

    Interfaces, Others, port_dict, switch_name = read_Cisco_SW()
    if debug:
        print(f"\nInterfaces = {Interfaces}")
        print(f"\nOthers = {Others}")
    return Interfaces, Others, port_dict, switch_dict


# Meraki exposes ONE 'vlan' field on a switch port, and what it means depends on
# the port type: native VLAN on a trunk, access VLAN on an access port.
_VLAN_SOURCE_BY_TYPE = {"trunk": "nativeVlan", "access": "dataVlan"}


def reconcile_port_vlan_args(port_args, intf_settings, port_label=""):
    """
    Make the 'vlan' and 'voiceVlan' args valid for this port's type.

    The encyclopedia can't express either rule: 'nativeVlan' and 'dataVlan'
    both map to meraki.field == 'vlan', so whichever key the pedia visits last
    wins regardless of port type, and nothing gates 'voiceVlan' on the type at
    all. Meraki rejects a voice VLAN on anything but an access port, and an
    action batch is atomic - one rejected action discards the whole batch - so
    a single mis-typed port silently loses every other port in the run.

    :param port_args: The Meraki args dict for this port, modified in place
    :param intf_settings: The port_dict entry for this interface
    :param port_label: The interface descriptor, used in the returned notes
    :return: List of human-readable notes about what was changed
    """
    notes = []
    port_type = port_args.get("type") or intf_settings.get("type") or "trunk"

    source_key = _VLAN_SOURCE_BY_TYPE.get(port_type)
    if source_key:
        value = intf_settings.get(source_key)
        if value in (None, ""):
            value = mc_pedia["port"][source_key]["meraki"].get("default", "1")
        port_args["vlan"] = value

    if port_type != "access" and port_args.get("voiceVlan") not in (None, ""):
        notes.append(
            f"{port_label}: dropped voice VLAN {port_args['voiceVlan']} - Meraki"
            + " supports a voice VLAN on access ports only"
        )
        port_args.pop("voiceVlan", None)

    return notes


def validate_port_action_args(port_args, port_label=""):
    """
    Return the reasons Dashboard would reject this updateDeviceSwitchPort body.

    Action batches are atomic, so an action Dashboard refuses takes every other
    action in the batch down with it. Catching the known-invalid shapes here
    costs us the one bad port instead of all of them.

    :param port_args: The Meraki args dict for this port
    :param port_label: The interface descriptor, used in the returned problems
    :return: List of human-readable problems; empty means the port looks valid
    """
    problems = []
    port_type = port_args.get("type")

    if port_type not in ("access", "trunk", "stack"):
        problems.append(f"{port_label}: invalid port type {port_type!r}")
    if port_type != "access" and port_args.get("voiceVlan") not in (None, ""):
        problems.append(f"{port_label}: voiceVlan is only valid on access ports")
    if port_type != "trunk" and port_args.get("allowedVlans") not in (None, ""):
        problems.append(f"{port_label}: allowedVlans is only valid on trunk ports")

    sticky = port_args.get("stickyMacAllowList")
    if sticky is not None and not isinstance(sticky, list):
        problems.append(
            f"{port_label}: stickyMacAllowList must be a list, got "
            + f"{type(sticky).__name__}"
        )

    return problems


# How long to keep asking Dashboard whether a submitted action batch landed.
# Batches go in with confirmed=True, synchronous=False, so a status read taken
# right after submission always shows completed=False, failed=False.
BATCH_POLL_INTERVAL_SECONDS = 2.0
BATCH_POLL_TIMEOUT_SECONDS = 300


def wait_for_action_batches(
    dashboard,
    organization_id,
    batch_ids,
    timeout=None,
    interval=None,
):
    """
    Poll submitted action batches until each one reaches a terminal state.

    :param dashboard: The Meraki dashboard API object
    :param organization_id: The Meraki organization ID
    :param batch_ids: List of action batch IDs to wait on
    :param timeout: Seconds to keep polling; defaults to the module constant
    :param interval: Seconds between rounds; defaults to the module constant
    :return: (succeeded_ids, {failed_id: [errors]}, [still_pending_ids])
    """
    debug = DEBUG or DEBUG_TRANSLATOR

    if timeout is None:
        timeout = BATCH_POLL_TIMEOUT_SECONDS
    if interval is None:
        interval = BATCH_POLL_INTERVAL_SECONDS

    pending = set(batch_ids)
    succeeded = []
    failed = {}
    deadline = time.monotonic() + timeout

    while pending and time.monotonic() < deadline:
        for batch_id in sorted(pending):
            try:
                batch = dashboard.organizations.getOrganizationActionBatch(
                    organization_id, batch_id
                )
            except meraki.APIError as api_exc:
                failed[batch_id] = [f"could not read batch status: {api_exc}"]
                pending.discard(batch_id)
                continue
            status = batch.get("status", {})
            if debug:
                print(f"Batch {batch_id} status = {status}")
            if status.get("failed"):
                failed[batch_id] = status.get("errors") or [
                    "batch failed, but Dashboard returned no detail"
                ]
                pending.discard(batch_id)
            elif status.get("completed"):
                succeeded.append(batch_id)
                pending.discard(batch_id)
        if pending:
            time.sleep(interval)

    return succeeded, failed, sorted(pending)


def reconcile_ports_against_batches(
    dashboard,
    organization_id,
    batch_ids,
    all_owners,
    conf_ports,
    unconf_ports,
    actions_per_batch=100,
):
    """
    Move ports out of conf_ports when their action batch did not apply.

    conf_ports is built from the actions we managed to *construct*; the SDK's
    batch helpers never touch the network, so nothing in it has been confirmed
    by Dashboard. Batches are atomic, so every port carried by a failed batch
    is unconfigured no matter how well its action was built.

    :param dashboard: The Meraki dashboard API object
    :param organization_id: The Meraki organization ID
    :param batch_ids: Submitted batch IDs, in submission order
    :param all_owners: (switch_num, port_id) per action, in the same order
    :      :           the actions were handed to the batch helper
    :param conf_ports: defaultdict(list) of ports we believe succeeded
    :param unconf_ports: defaultdict(list) of ports we know did not
    :param actions_per_batch: The helper's actions_per_new_batch value
    :return: NONE - modifies conf_ports and unconf_ports in place
    """
    if not batch_ids:
        return

    if mc_meraki_dry_run.MERAKI_DRY_RUN:
        print("[DRY-RUN] Not polling action batch status.")
        return

    print("Confirming with Dashboard that the action batches applied...")
    succeeded, failed, still_pending = wait_for_action_batches(
        dashboard, organization_id, batch_ids
    )

    if not failed and not still_pending:
        return

    debug_batches = DEBUG or DEBUG_TRANSLATOR

    def _demote(batch_index, reason):
        """Move every port carried by this batch into unconf_ports."""
        start = batch_index * actions_per_batch
        for switch_num, port_id in all_owners[start:start + actions_per_batch]:
            if port_id in conf_ports[switch_num]:
                conf_ports[switch_num].remove(port_id)
            if port_id not in unconf_ports[switch_num]:
                unconf_ports[switch_num].append(port_id)
        if debug_batches:
            print(f"Demoted the ports in batch {batch_index} ({reason}).")

    for index, batch_id in enumerate(batch_ids):
        if batch_id in failed:
            print(
                f"Dashboard rejected action batch {batch_id}. It was atomic, "
                + "so none of the ports it carried were configured:"
            )
            for error in failed[batch_id]:
                print(f"  - {error}")
            _demote(index, "failed")
        elif batch_id in still_pending:
            print(
                f"Action batch {batch_id} had still not finished after "
                + f"{BATCH_POLL_TIMEOUT_SECONDS} seconds, so we cannot confirm "
                + "its ports. Action batches have no Dashboard UI - check it "
                + f"with GET /organizations/{organization_id}/actionBatches/"
                + f"{batch_id}"
            )
            _demote(index, "unconfirmed")

    if succeeded and (failed or still_pending):
        print(f"{len(succeeded)} of {len(batch_ids)} action batches applied cleanly.")


def model_port_count(model):
    """
    Best-effort front-panel port count for a Meraki or Catalyst model number.

    Both vendors put the downlink count right after the first dash: MS225-48LP,
    C9300-48UXM, C9200L-24PXG-4X. It is a convention, not a guarantee - a
    C9200CX-8P-2X2G has more than 8 front-panel ports - so callers must treat
    a disagreement as "look closer", not as fact.
    :param model: Model string from the Dashboard inventory
    :return: Port count as an int, or None if the model doesn't carry one
    """
    if not model:
        return None
    match = re.search(r"-(\d{1,2})", model)
    if match is None:
        return None
    return int(match.group(1))


def summarize_parsed_ports(port_dict, Intf_list):
    """
    Group the parsed Catalyst ports by stack member for the port count check.

    :param port_dict: Dictionary of IOSXE ports and features from Evaluate
    :param Intf_list: List of standard ports & L3 interfaces to configure
    :return: Dict of switch number (1-based, int) -> dict with the downlink
    :      : port names, the uplink/module port names we are skipping, and a
    :      : per-interface-type count of the downlinks
    """
    summary = defaultdict(
        lambda: {"downlinks": [], "uplinks": [], "types": defaultdict(int)}
    )
    for port in Intf_list:
        if port.startswith("Vlan"):
            continue
        settings = port_dict[port]
        try:
            switch_num = int(settings.get("sw_module") or 1)
        except ValueError:
            switch_num = 1
        entry = summary[switch_num]
        if settings.get("sub_module") == "0":
            entry["downlinks"].append(port)
            entry["types"][re.sub(r"\d+|/", "", port)] += 1
        else:
            entry["uplinks"].append(port)
    return summary


def check_port_counts(
    dashboard, organization_id, sw_list, port_dict, Intf_list, debug=False
):
    """
    Confirm the Catalyst downlinks we parsed match the target Meraki switches.

    A disagreement almost always means we mis-parsed the config rather than
    that the hardware is wrong - especially when migrating a switch onto its
    own Cloud ID, where source and target are the same physical box. So print
    everything needed to tell the two apart instead of a bare one-liner: the
    stack member to serial mapping, the model we compared against, and the
    interfaces we counted, skipped, and dropped.
    :param dashboard: Active Meraki dashboard API session to use
    :param organization_id: Meraki Org ID the target switches live in
    :param sw_list: List of Meraki switch serial numbers to configure
    :param port_dict: Dictionary of IOSXE ports and features from Evaluate
    :param Intf_list: List of standard ports & L3 interfaces to configure
    :param debug: When True, print the parsed detail even when it all matches
    :return: NONE - exits unless every switch matches or the check is ignored
    """
    summary = summarize_parsed_ports(port_dict, Intf_list)
    if debug:
        for switch_num in sorted(summary):
            entry = summary[switch_num]
            print(
                f"switch {switch_num}: {len(entry['downlinks'])} downlinks "
                + f"{dict(entry['types'])}, "
                + f"{len(entry['uplinks'])} uplink/module ports"
            )

    rows = []
    problems = []
    for index, serial in enumerate(sw_list):
        switch_num = index + 1
        entry = summary.get(switch_num)
        parsed = len(entry["downlinks"]) if entry else 0
        try:
            model = dashboard.organizations.getOrganizationInventoryDevice(
                organization_id, serial
            )["model"]
        except Exception as error:
            print(
                f"Couldn't look up serial number {serial} in org "
                + f"{organization_id}: {error}"
            )
            print(
                "That serial has to be claimed into this organization before "
                + "we can translate to it."
            )
            sys.exit()
        port_max = model_port_count(model)
        rows.append((switch_num, serial, model, port_max, parsed))
        if port_max is None:
            problems.append(
                f"Switch {switch_num} ({serial}, {model}): couldn't read a "
                + "port count out of the model number, so we can't check it."
            )
        elif port_max != parsed:
            problems.append(
                f"Switch {switch_num} ({serial}) is a {model} with {port_max} "
                + f"ports, but we parsed {parsed} port(s) for switch "
                + f"{switch_num} out of the Catalyst config."
            )

    # Config referring to stack members we have no target switch for is its
    # own failure, and a likely cause of a count of 0 above.
    extra = [num for num in sorted(summary) if num > len(sw_list)]
    if extra:
        problems.append(
            "The Catalyst config has ports on switch(es) "
            + ", ".join(str(num) for num in extra)
            + f", but only {len(sw_list)} target serial number(s) were given: "
            + ", ".join(str(s) for s in sw_list)
        )

    if not problems:
        return

    print("\nPort count check failed:")
    for problem in problems:
        print(f"  - {problem}")
    print("\nWhat we parsed out of the Catalyst config:")
    for switch_num, serial, model, port_max, parsed in rows:
        entry = summary.get(switch_num) or {"types": {}, "uplinks": []}
        print(
            f"  switch {switch_num}  serial {serial}  model {model}  "
            + f"model ports {port_max}  parsed downlinks {parsed}"
        )
        if entry["types"]:
            types = ", ".join(f"{k} x{v}" for k, v in sorted(entry["types"].items()))
            print(f"      counted by type: {types}")
        if entry["uplinks"]:
            print(
                "      skipped as uplink/module ports: "
                + ", ".join(entry["uplinks"])
            )
    for switch_num in sorted(summary):
        if switch_num > len(sw_list):
            entry = summary[switch_num]
            print(
                f"  switch {switch_num}  NO TARGET SERIAL  "
                + f"parsed downlinks {len(entry['downlinks'])}"
            )
    print(
        "\nUsual causes, most common first:\n"
        + "  1. A port type we don't recognize, so its ports were dropped -\n"
        + "     look for a NOTE about untranslated interfaces above.\n"
        + "  2. A mixed-model stack whose serial numbers were given in a\n"
        + "     different order than the switch numbers in the config.\n"
        + "  3. A partial 'show running-config' capture, so some interfaces\n"
        + "     never made it into the .cfg file in the files folder.\n"
        + "  4. A model whose port count isn't the number in its name.\n"
    )
    if IGNORE_PORT_COUNT:
        print(
            "Continuing anyway because --ignore-port-count was given. Ports "
            + "we didn't parse will not be configured.\n"
        )
        return
    print(
        "Stopping. Re-run with --ignore-port-count to translate the ports we "
        + "did parse, or with DEBUG_TRANSLATOR set for the full parse.\n"
    )
    sys.exit()


def MerakiConfig(
    dashboard,
    organization_id,
    switch_path,
    sw_list,
    port_dict,
    Intf_list,
    Other_list,
    switch_dict,
    nm_list,
    unified_os,
    meraki_api_key,
):
    """
    This parent function will convert Catalyst switch config features to
    Meraki features and send them to Dashboard to program Meraki switches.
    :param dashboard: Active Meraki dashboard API session to use
    :param organization_id: Meraki Org ID used in batch configuration
    :param sw_list: List of Meraki switch serial numbers to configure
    :param port_dict: Dictionary of IOSXE ports and features from Evaluate
    :param Intf_list: List of standard ports & L3 interfaces to configure
    :param Other_list: List of other ports to configure (Port-channels)
    :param switch_name: The catalyst IOSXE hostname
    :param nm_list: List of Network Modules per switch
    :return: Lists of configured and unconfigured ports, and a list with a URL
    :      :  to each of the Meraki switches that we configured or modified
    """

    debug = DEBUG or DEBUG_TRANSLATOR

    # Make sure that the switches we are translating to have the same
    # number of ports
    check_port_counts(dashboard, organization_id, sw_list, port_dict, Intf_list, debug)

    # Create batch action lists
    action_list = list()
    all_actions = list()
    # Kept in lockstep with action_list / all_actions so that when a batch
    # fails we can name the ports it was carrying. Each entry is
    # (switch_num, port_id) for the action at the same index.
    owner_list = list()
    all_owners = list()
    returns_dict = {}
    post_ports_list = list()
    # Create good and bad port lists
    conf_ports = defaultdict(list)
    unconf_ports = defaultdict(list)

    if debug:
        print(f"Other_list = {Other_list}")
        for port in Other_list:
            if port in port_dict.keys():
                print(f"port_dict['{port}'] = {port_dict[port]}")

    # Create a place to hold all of the arguments to send to Dashboard
    # to update a switch port
    args = list(dict())

    # Loop to go through all the ports of the switches
    def loop_configure_meraki(
        port_dict, Intf_list, switch_dict, unified_os, meraki_api_key
    ):
        """
        This sub-function does the actual work of setting the meraki functions
        based on the dictionary of IOSXE ports and features from Evaluate.
        :param port_dict: Dictionary of IOSXE ports and features from Evaluate
        :param Intf_list: The list of ports to configure
        :param switch_name: The catalyst IOSXE hostname
        :return: NONE - modifies global variables in the parent function
        """

        # For encyclopedia exec() snippets (e.g. L3 routing HTTP in mc_pedia2).
        MERAKI_DRY_RUN = mc_meraki_dry_run.MERAKI_DRY_RUN
        meraki_requests_request = mc_meraki_dry_run.meraki_requests_request

        # Create a place to hold the Dashboard URL for each switch
        # Configure the switch_name in the Dashboard
        if debug:
            print(f"switch_dict = {switch_dict}")
        for key, val in mc_pedia["switch"].items():
            if not switch_dict[key] == []:
                if (
                    val["translatable"] == "✓"
                    and val["meraki"]["skip"] == "post_process"
                ) or val["meraki"]["skip"] == "post_ports":
                    if debug:
                        print(f"key = {key}, val = {val}")
                    if val["meraki"]["skip"] == "post_ports":
                        post_ports_list.append([key, True])
                    newvals = {}
                    exec(val["meraki"].get("post_process"), locals(), newvals)
                    if debug:
                        print(f"newvals = {newvals}")
                    try:
                        return_vals = newvals["return_vals"]
                        if debug:
                            print(
                                f"newvals['return_vals'] = " + f"{newvals['return_vals']}"
                            )
                        n = 0
                        while n < len(return_vals):
                            try:
                                switch_dict[return_vals[n]] = newvals[return_vals[n]]
                                returns_dict[return_vals[n]] = newvals[return_vals[n]]
                            except KeyError:
                                if debug:
                                    print(f"KeyError: '{return_vals[n]}' not found in newvals")
                            n += 1
                    except KeyError:
                        if debug:
                            print("KeyError: 'return_vals' not found in newvals")
                    if debug:
                        print(f"switch_dict = {switch_dict}")

        # Loop to get all the interfaces in the port_dict
        y = 0
        while y <= len(Intf_list) - 1:
            interface_descriptor = Intf_list[y]
            intf_settings = port_dict[interface_descriptor]
            if debug:
                print("\n----------- " + interface_descriptor + " -----------")
                pprint.pprint(intf_settings)

            # Check the switch that mapped to those catalyst ports
            try:
                switch_num = int(intf_settings["sw_module"])
            except:
                switch_num = 1
            if not switch_num == 0:
                switch_num -= 1

            if "Vlan" not in interface_descriptor:
                # Setup the features for a physical interface
                if intf_settings["sub_module"] == "1":
                    if not nm_list[switch_num] == "":
                        for regex in nm_dict[nm_list[switch_num]]["ports"]:
                            if re.match(regex, interface_descriptor):
                                if debug:
                                    print(
                                        f"I matched {interface_descriptor}"
                                        + f" to {regex}\n"
                                    )
                                port = "1_" + nm_list[switch_num]
                                port += "_" + intf_settings["port"]
                                args.append([sw_list[switch_num], port, {}])
                else:
                    args.append([sw_list[switch_num], intf_settings["port"], {}])
                # Setup the default features
                args[y][2].update(
                    {
                        "enabled": True,
                        "tags": [],
                        "poeEnabled": True,
                        "isolationEnable": False,
                        "rstpEnabled": True,
                        "accessPolicyType": "Open",
                    }
                )
                for key, val in mc_pedia["port"].items():
                    newvals = {}
                    # Some encyclopedia keys (e.g. nativeVlan/dataVlan) map
                    # to the same underlying Meraki API field, since Meraki
                    # exposes one 'vlan' param for both access and native
                    # VLAN. Use that mapping instead of the pedia key name
                    # when talking to the API.
                    field = val["meraki"].get("field", key)
                    if val["meraki"]["skip"] is not True:
                        # Apply any post processing for Meraki config
                        if val["meraki"]["skip"] in ["post_process", "post_ports"]:
                            exec(val["meraki"].get("post_process"), locals(), newvals)
                            if debug:
                                print(f"newvals = {newvals}")
                            if val["meraki"]["skip"] == "post_process":
                                if not newvals[key] == "":
                                    # Apply post processing port setting
                                    # returned for that key
                                    intf_settings[key] = newvals[key]
                        try:
                            # Update the features we will later apply
                            # for this interface
                            args[y][2].update({field: intf_settings[key]})
                        except:
                            if "default" in val["meraki"] and field not in args[y][2]:
                                # We weren't given a value for this feature,
                                # and no sibling key already supplied a real
                                # value for the same API field, so apply the
                                # Meraki default value
                                intf_settings[key] = val["meraki"]["default"]
                                args[y][2].update({field: intf_settings[key]})
                        if "return_vals" in newvals:
                            # We must have called a post-process feature in
                            # the encyclopedia that also has a
                            # post-port-process, so let's keep the extra
                            # return values for that extra process
                            return_vals = newvals["return_vals"]
                            if debug:
                                print(
                                    "newvals['return_vals'] = "
                                    + f"{newvals['return_vals']}"
                                )
                            n = 0
                            while n < len(return_vals):
                                if debug:
                                    print(f"return_vals[{n}] = " + f"{return_vals[n]}")
                                    print("post_ports_list = " + f"{post_ports_list}")
                                # Data returned for post_ports_processing
                                # looks like:
                                # encyclopedia_key, [a list of values]
                                # There can be multiple instances, so we will
                                # append them all to a list of lists
                                try:
                                    post_ports_list.append(
                                        [return_vals[n], newvals[return_vals[n]]]
                                    )
                                except KeyError:
                                    if debug:
                                        print(f"KeyError: '{return_vals[n]}' not found in newvals")
                                if debug:
                                    print("post_ports_list = " + f"{post_ports_list}")
                                n += 1

                # The pedia loop above is key-ordered, not type-aware, so fix
                # up the port-type-dependent args now that every key has run.
                vlan_notes = reconcile_port_vlan_args(
                    args[y][2], intf_settings, interface_descriptor
                )
                for note in vlan_notes:
                    print(f"Note: {note}")
                if vlan_notes:
                    intf_settings.setdefault("translation_notes", []).extend(
                        vlan_notes
                    )

                try:
                    # If port was disabled, disable it in the port)_dict
                    args[y][2].update(
                        {
                            "enabled": (
                                False if intf_settings["active"] == "false" else True
                            )
                        }
                    )
                except:
                    pass
                # Check if the interface mode is configured as Access
                if intf_settings["type"] == "access":
                    # For access ports, apply port security and sticky MACs
                    # as needed
                    try:
                        if not intf_settings["mac"] == []:
                            pass
                    except:
                        pass
                    try:
                        if not intf_settings["Port_Sec"] == "":
                            mac_limit = intf_settings["Port_Sec"]
                    except:
                        intf_settings["Port_Sec"] = ""
                    if not intf_settings["Port_Sec"] == "":
                        args[y][2].update(
                            {
                                "accessPolicyType": "Sticky MAC allow list",
                                # The API wants an array here, not a JSON string
                                "stickyMacAllowList": intf_settings["mac"],
                                "stickyMacAllowListLimit": mac_limit,
                            }
                        )

            else:
                # Setup the features for a logical L3 interface
                # Setup the default features
                stack_id = ""
                if "switchStackId" in returns_dict:
                    stack_id = returns_dict["switchStackId"]
                args.append(
                    [
                        returns_dict["networkId"],
                        stack_id,
                        interface_descriptor,
                        intf_settings["vlan"],
                        {},
                    ]
                )
                for key, val in mc_pedia["layer3"].items():
                    newvals = {}
                    if val["meraki"]["skip"] is not True:
                        # Apply any post processing for Meraki config
                        if val["meraki"]["skip"] in ["post_process", "post_ports"]:
                            exec(val["meraki"].get("post_process"), locals(), newvals)
                            if debug:
                                print(f"newvals = {newvals}")
                            if val["meraki"]["skip"] == "post_process":
                                try:
                                    if not newvals[key] == "":
                                        # Apply post processing port setting
                                        # returned for that feature
                                        intf_settings[key] = newvals[key]
                                except KeyError:
                                    if debug:
                                        print(
                                            f"KeyError: {key} not found in newvals for {interface_descriptor}"
                                        )
                        if debug:
                            print(f"key = {key}, " + f"newvals[key] = {newvals.get(key, 'NOT FOUND')}")
                        try:
                            # Update the features we will later apply
                            # for this interface
                            args[y][4].update({key: intf_settings[key]})
                        except:
                            if "default" in val["meraki"]:
                                # We weren't given a value for this feature,
                                # apply the Meraki default value
                                intf_settings[key] = val["meraki"]["default"]
                                args[y][4].update({key: intf_settings[key]})
                        if "return_vals" in newvals:
                            # We must have called a post_process feature in
                            # the encyclopedia that also has a
                            # post_port_process, so let's keep the extra
                            # return values for that extra process
                            return_vals = newvals["return_vals"]
                            if debug:
                                print(
                                    "newvals['return_vals'] = "
                                    + f"{newvals['return_vals']}"
                                )
                            # On L3 interfaces, save the extra return values
                            # in the port_dict for that port for
                            # post_ports processing
                            n = 0
                            while n < len(return_vals):
                                if debug:
                                    print(f"return_vals[{n}] = " + f"{return_vals[n]}")
                                try:
                                    args[y][4].update(
                                        {return_vals[n]: newvals[return_vals[n]]}
                                    )
                                except KeyError:
                                    if debug:
                                        print(f"KeyError: '{return_vals[n]}' not found in newvals")
                                if debug:
                                    print(
                                        f"args for {interface_descriptor}"
                                        + f" = {args[y][4]}"
                                    )
                                n += 1
                if debug:
                    print(f"args[{y}] = {args[y]}")
            # Append args to the port_dict as meraki_args
            port_dict[interface_descriptor]["meraki_args"] = args[y]

            # Append the port update call to Dashboard to the batch list
            if debug:
                print("Number of sub lists in action_list is " + f"{len(action_list)}")
                try:
                    print(
                        "Number of batch actions in action_list"
                        + f"[{switch_num}] is {len(action_list[switch_num])}"
                    )
                except:
                    pass

            if not len(action_list) == switch_num + 1:
                # We are on to the next switch, so I want a new sublist
                action_list.append([])
                owner_list.append([])
            # If it's a physical interface,
            # Add this action to the action_list sublist for the switch
            if "Vlan" not in interface_descriptor:
                # An action batch is all-or-nothing, so a port Dashboard would
                # reject costs us every other port in the batch. Leave it out
                # and report it rather than gamble the whole run on it.
                problems = validate_port_action_args(
                    args[y][2], interface_descriptor
                )
                if problems:
                    for problem in problems:
                        print(f"Not translating {problem}")
                    unconf_ports[switch_num].append(args[y][1])
                else:
                    try:
                        action_list[switch_num].append(
                            dashboard.batch.switch.updateDeviceSwitchPort(
                                args[y][0], args[y][1], **args[y][2]
                            )
                        )
                        owner_list[switch_num].append((switch_num, args[y][1]))
                    except Exception as action_exc:
                        unconf_ports[switch_num].append(args[y][1])
                        print(
                            "We caught an exception configuring "
                            + f"{args[y][1]} on {switch_num}: {action_exc}"
                        )
                    if not args[y][1] in unconf_ports[switch_num]:
                        conf_ports[switch_num].append(args[y][1])
            y += 1

        # post_ports processing
        if debug:
            print(f"At post_ports, port_dict = {port_dict}")
            print(f"Length of post_ports_list = {len(post_ports_list)}")
            print(f"post_ports_list = {post_ports_list}")
        if not len(post_ports_list) == 0:
            # In case there are multiple entries for the same
            # post_ports_process, create a consolidated list so that we only
            # call each one once
            short_list = list(set(map(lambda x: x[0], post_ports_list)))
            if debug:
                print(f"short_list = {short_list}")

            # This is the processing loop for port-level post ports processes
            item = 0
            while item < len(short_list):
                if debug:
                    print(f"short_list[{item}] = {short_list[item]}")
                # Call the post-port process for that feature if
                # there is one in the PORTS section of the encyclopedia
                newvals = {}
                if short_list[item] in mc_pedia["port"].keys():
                    try:
                        exec(
                            mc_pedia["port"][short_list[item]]["meraki"].get(
                                "post_ports_process"
                            ),
                            locals(),
                            newvals,
                        )
                    except Exception as pp_exc:
                        # The encyclopedia is exec'd source that can be
                        # replaced from upstream underneath us, so never let
                        # one bad snippet take down the whole translation.
                        print(
                            "We caught an exception in the post-port process "
                            + f"for {short_list[item]}: {pp_exc}"
                        )
                    if "return_vals" in newvals:
                        return_vals = newvals["return_vals"]
                        if "channel_port_dict" in return_vals and "channel_port_dict" in newvals:
                            if debug:
                                print(
                                    "newvals['channel_port_dict'] = "
                                    + f"{newvals['channel_port_dict']}"
                                )
                            for a_port in newvals["channel_port_dict"]:
                                if debug:
                                    print(f"a_port = {a_port}")
                                port_dict[a_port] = newvals["channel_port_dict"][a_port]
                                if debug:
                                    print(
                                        "port_dict[a_port] = " + f"{port_dict[a_port]}"
                                    )
                        if "conf_ports" in return_vals and "conf_ports" in newvals:
                            cp_list = newvals["conf_ports"]
                            if debug:
                                print(f"cp_list = {cp_list}")
                            for port in cp_list:
                                switch_num = "stack" if len(sw_list) > 1 else 0
                                conf_ports[switch_num].append(port)
                        if "unconf_ports" in return_vals and "unconf_ports" in newvals:
                            up_list = newvals["unconf_ports"]
                            if debug:
                                print(f"up_list = {up_list}")
                            for port in up_list:
                                switch_num = "stack" if len(sw_list) > 1 else 0
                                unconf_ports[switch_num].append(port)
                item += 1

            # This is used for switch-level post ports processes
            item = 0
            if debug:
                print(f"sw_list = {sw_list}")
                print(f"locals() = {locals()}")
            while item < len(short_list):
                if debug:
                    print(f"short_list[{item}] = {short_list[item]}")
                # Call the post-port process for that feature if
                # there is one in the SWITCH section of the encyclopedia
                newvals = {}
                if short_list[item] in mc_pedia["switch"].keys():
                    exec(
                        mc_pedia["switch"][short_list[item]]["meraki"].get(
                            "post_ports_process"
                        ),
                        locals(),
                        newvals,
                    )
                    if debug:
                        print(f"return_vals = {return_vals}")
                    if "return_vals" in newvals:
                        return_vals = newvals["return_vals"]
                        pass
                item += 1

        if debug:
            print(f"\n\naction_list = {action_list}\n\n")

    loop_configure_meraki(port_dict, Intf_list, switch_dict, unified_os, meraki_api_key)

    # Combine all of the action_list sublists into a larger set for batching
    x = 0
    while x <= len(action_list) - 1:
        all_actions.extend(action_list[x])
        # Flattened in the same order as all_actions, so all_owners[i] is the
        # (switch_num, port_id) that produced all_actions[i]
        if x <= len(owner_list) - 1:
            all_owners.extend(owner_list[x])
        x += 1
    if debug:
        print(f"all_actions = {all_actions}")

    # Save an action batch file for the port features
    # dir = os.path.join(os.getcwd(), DEFAULT_FILES_FOLDER)
    # with open(os.path.join(dir,switch_path+".ab0"), 'w') as file:
    #    file.write(json.dumps(all_actions)) # use `json.loads` to reverse
    #    file.close()

    if debug:
        print(f"Number of batch actions for Dashboard: {len(action_list)}\n")
        print(f"Args listdict is: {args}\n")

    test_helper = batch_helper.BatchHelper(
        dashboard,
        organization_id,
        all_actions,
        linear_new_batches=False,
        actions_per_new_batch=100,
    )
    # prepare() only groups actions locally - no API traffic - so it is safe
    # to run in either mode.
    test_helper.prepare()
    # test_helper.generate_preview()
    if mc_meraki_dry_run.MERAKI_DRY_RUN:
        # execute() polls the live action batch queue before submitting, which
        # is pointless when the submission itself is going to be swallowed and
        # needs org-level API rights the run may not have. Report instead.
        print(
            f"[DRY-RUN] Not submitting {len(all_actions)} port action(s) in "
            + f"{len(test_helper.new_batches)} action batch(es)."
        )
    else:
        test_helper.execute()
    if debug:
        print(f"helper status is {test_helper.status}")

    # conf_ports so far only records that we could *build* each action -
    # dashboard.batch.* never touches the network. Batches are submitted
    # asynchronously and are atomic, so reconcile against what Dashboard
    # actually did with them before we claim any of these ports succeeded.
    reconcile_ports_against_batches(
        dashboard,
        organization_id,
        test_helper.submitted_new_batches_ids,
        all_owners,
        conf_ports,
        unconf_ports,
        actions_per_batch=100,
    )

    if debug:
        print(f"\nport_dict = {port_dict}\n")

    return (
        conf_ports,
        unconf_ports,
        port_dict,
        returns_dict["urls"],
        returns_dict["networkId"],
    )
