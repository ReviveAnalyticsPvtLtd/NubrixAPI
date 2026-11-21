import ipaddress
import socket

from pymongo.uri_parser import parse_uri

BLOCKED_ADDRESS_MESSAGE = "This address can't be reached from Nubrix."


class BlockedAddressError(ValueError):
    pass


def publicAddress(host: str, port: int) -> str:
    """Return the first resolved IP of host when every resolved IP is global, else raise."""
    name = host.strip().rstrip(".").lower()
    if name == "localhost" or name.endswith(".internal"):
        raise BlockedAddressError(f"Host {host} is not allowed")
    # a name that does not resolve raises socket.gaierror: the loaders answer it as a failed connection
    found = socket.getaddrinfo(name, port, proto=socket.IPPROTO_TCP)
    # drop an IPv6 zone id ("fe80::1%eth0"), which ip_address() rejects
    addresses = [item[4][0].split("%")[0] for item in found]
    if not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise BlockedAddressError(f"Host {host} resolves to a non-public address")
    return addresses[0]


def checkMongoUri(uri: str) -> None:
    """Raise BlockedAddressError when any host in the URI (SRV included) is not public.
    A URI or name that cannot be parsed or resolved raises its own error, not BlockedAddressError.

    shortcut: the driver resolves the name again when it connects, so a DNS answer that
    changes in between is not caught; pin the address if that ever matters.
    """
    for host, port in parse_uri(uri)["nodelist"]:
        publicAddress(host, port or 27017)
