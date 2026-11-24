import ipaddress
import socket

from pymongo.uri_parser import parse_uri

BLOCKED_ADDRESS_MESSAGE = "This address can't be reached from Nubrix."


class BlockedAddressError(ValueError):
    """Its message never names the host: a mistyped URI can put part of the password where the host goes."""


def publicAddress(host: str, port: int) -> str:
    """Return the first resolved IP of host when every resolved IP is global, else raise."""
    name = host.strip().rstrip(".").lower()
    if name == "localhost" or name.endswith(".internal"):
        raise BlockedAddressError("Host is not allowed")
    # a name that does not resolve raises socket.gaierror: the loaders answer it as a failed connection
    found = socket.getaddrinfo(name, port, proto=socket.IPPROTO_TCP)
    # drop an IPv6 zone id ("fe80::1%eth0"), which ip_address() rejects
    addresses = [item[4][0].split("%")[0] for item in found]
    if not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise BlockedAddressError("Host resolves to a non-public address")
    return addresses[0]


def publicServers(server_descriptions):
    """pymongo server_selector: keep only servers whose announced address is public.

    A replica set can announce hosts the URI never named (private IPs, *.internal), and the
    driver would connect to them.
    """
    kept = []
    for description in server_descriptions:
        try:
            publicAddress(*description.address)
        except (BlockedAddressError, OSError):
            continue
        kept.append(description)
    return kept


def checkMongoUri(uri: str) -> None:
    """Raise BlockedAddressError when any host in the URI (SRV included) is not public.
    A URI or name that cannot be parsed or resolved raises its own error, not BlockedAddressError.

    shortcut: the driver's own monitoring hello may still reach a discovered host before
    publicServers excludes it, and it resolves names again when it connects (the answer can
    change after this check); upgrade by pinning addresses if that ever matters.
    """
    for host, port in parse_uri(uri)["nodelist"]:
        publicAddress(host, port or 27017)
