import meraki


def get_switch_config(dashboard_api: meraki.DashboardAPI, serial: str) -> str:
    """
    Return a formatted summary of a switch's ports and layer 3 routing
    interfaces as currently recorded in the Meraki Dashboard API. This
    reads Meraki's cloud-side config record for the device, so it works
    even when the device itself is offline or unreachable.

    Parameters:
    dashboard_api (meraki.DashboardAPI): Active dashboard API client.
    serial (str): Meraki device serial number.

    Returns:
    str: Markdown-friendly summary of ports and routing interfaces, or a
    message describing why it could not be retrieved.
    """
    serial = serial.strip()
    if serial == "":
        return "I'm sorry, but I don't have a valid Meraki device serial number."

    try:
        ports = dashboard_api.switch.getDeviceSwitchPorts(serial)
    except meraki.exceptions.APIError as err:  # type: ignore
        return f"I'm sorry, but I was unable to get switch ports for {serial}: {err}"

    try:
        interfaces = dashboard_api.switch.getDeviceSwitchRoutingInterfaces(serial)
    except meraki.exceptions.APIError as err:  # type: ignore
        return (
            f"I'm sorry, but I was unable to get routing interfaces for "
            f"{serial}: {err}"
        )

    lines = [f"Dashboard config on record for **{serial}**:", ""]

    lines.append(f"Ports ({len(ports)}):")
    for port in sorted(ports, key=lambda p: p.get("portId", "")):
        port_id = port.get("portId", "?")
        port_type = port.get("type", "?")
        vlan = port.get("vlan", "-")
        voice_vlan = port.get("voiceVlan", "-")
        enabled = port.get("enabled", "-")
        lines.append(
            f"- Port {port_id}: type={port_type}, vlan={vlan}, "
            f"voiceVlan={voice_vlan}, enabled={enabled}"
        )

    lines.append("")
    lines.append(f"Layer 3 routing interfaces ({len(interfaces)}):")
    if len(interfaces) == 0:
        lines.append("- (none)")
    for intf in interfaces:
        lines.append(
            f"- {intf.get('name', '?')}: vlanId={intf.get('vlanId', '?')}, "
            f"interfaceIp={intf.get('interfaceIp', '?')}, "
            f"subnet={intf.get('subnet', '?')}"
        )

    return "\n".join(lines)
