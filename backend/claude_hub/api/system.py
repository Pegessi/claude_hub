import ipaddress
import re
import socket
import subprocess

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..models import User

router = APIRouter(prefix="/api/system", tags=["system"])


class NetworkAddress(BaseModel):
    address: str
    label: str


class NetworkAccessResponse(BaseModel):
    hostname: str
    addresses: list[NetworkAddress]


IFCONFIG_INET_RE = re.compile(r"\binet\s+(?:addr:)?(?P<address>\d+\.\d+\.\d+\.\d+)\b")
IP_ADDR_RE = re.compile(r"\binet\s+(?P<address>\d+\.\d+\.\d+\.\d+)/\d+\b")


def _is_usable_ipv4(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False

    return (
        parsed.version == 4
        and not parsed.is_loopback
        and not parsed.is_link_local
        and not parsed.is_multicast
        and not parsed.is_unspecified
    )


def _hostname_ipv4_addresses() -> set[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        addrinfo = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return addresses

    for entry in addrinfo:
        sockaddr = entry[4]
        if sockaddr:
            address = sockaddr[0]
            if isinstance(address, str):
                addresses.add(address)
    return addresses


def _default_route_ipv4_addresses() -> set[str]:
    addresses: set[str] = set()
    for destination in ("8.8.8.8", "1.1.1.1", "192.0.2.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.2)
                sock.connect((destination, 80))
                addresses.add(sock.getsockname()[0])
        except OSError:
            continue
    return addresses


def _interface_ipv4_addresses() -> dict[str, str]:
    return _ip_command_ipv4_addresses() | _ifconfig_ipv4_addresses()


def _ip_command_ipv4_addresses() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    if result.returncode != 0:
        return {}

    addresses: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        interface = parts[1] if len(parts) > 1 else "LAN IP"
        match = IP_ADDR_RE.search(line)
        if match:
            addresses[match.group("address")] = interface.rstrip(":")
    return addresses


def _ifconfig_ipv4_addresses() -> dict[str, str]:
    result: subprocess.CompletedProcess[str] | None = None
    for command in (["ifconfig"], ["/sbin/ifconfig"]):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            break

    if result is None or result.returncode != 0:
        return {}

    addresses: dict[str, str] = {}
    interface = "LAN IP"
    for line in result.stdout.splitlines():
        if line and not line.startswith((" ", "\t")):
            interface = line.split(":", 1)[0].strip() or "LAN IP"
        match = IFCONFIG_INET_RE.search(line)
        if match:
            addresses[match.group("address")] = interface
    return addresses


def _local_network_addresses() -> list[NetworkAddress]:
    interface_addresses = _interface_ipv4_addresses()
    candidates = (
        set(interface_addresses.keys())
        | _hostname_ipv4_addresses()
        | _default_route_ipv4_addresses()
    )
    addresses = sorted(
        {address for address in candidates if _is_usable_ipv4(address)},
        key=lambda address: int(ipaddress.IPv4Address(address)),
    )
    return [
        NetworkAddress(address=address, label=interface_addresses.get(address, "LAN IP"))
        for address in addresses
    ]


@router.get("/network-access", response_model=NetworkAccessResponse)
async def get_network_access(
    current_user: User = Depends(get_current_user),
) -> NetworkAccessResponse:
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "localhost"

    return NetworkAccessResponse(hostname=hostname, addresses=_local_network_addresses())
