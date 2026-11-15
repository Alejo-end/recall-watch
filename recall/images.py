"""Keep a copy of each product photo, so the evidence survives when a shop or the authority changes it.

Two kinds of photo are kept: the photos that the Food Authority publishes with a notice, and the
product photo that Context.dev's product extraction found on a watched page.  A photo is stored as
bytes with its SHA-256, next to the database.  Hotlinking the original would show whatever the
host serves today, not what it served when we looked.

The address of a page photo comes from the page itself, so the download is guarded: https only,
no private or local addresses, raster image types only (an SVG can carry script), and a size cap.
The connection goes to the IP address that passed the check, so a second DNS answer cannot point
the download at a private address.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

MAX_BYTES = 5 * 1024 * 1024
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}


class ImageRefused(Exception):
    pass


def public_address(host: str) -> str | None:
    """One address of `host`, only when every address it resolves to is public. None otherwise."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or any(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                            or ip.is_unspecified for ip in addresses):
        return None
    return str(addresses[0])


def fetch_image(url: str, client: httpx.Client | None = None, hops: int = 3) -> tuple[bytes, str]:
    """Returns (bytes, file extension). Raises ImageRefused when the address or the answer is not a safe image.

    Redirects are followed by hand, at most `hops` times, and every hop is checked like the first.
    """
    own = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False, headers={"User-Agent": "RecallWatch/0.1"})
    try:
        for _ in range(hops + 1):
            parsed = urlparse(url)
            ip = public_address(parsed.hostname) if parsed.scheme == "https" and parsed.hostname else None
            if not ip:
                raise ImageRefused("only public https image addresses are fetched")
            # Connect to the address that was checked, not to a second DNS answer (DNS rebinding).
            # TLS still verifies the certificate against the real host name, through SNI.
            netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{parsed.port}" if parsed.port else "")
            pinned = parsed._replace(netloc=netloc).geturl()
            with client.stream("GET", pinned, headers={"Host": parsed.netloc.rsplit("@", 1)[-1]},
                               extensions={"sni_hostname": parsed.hostname}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = str(httpx.URL(url).join(r.headers["location"]))
                    continue
                if r.status_code != 200:
                    raise ImageRefused(f"HTTP {r.status_code}")
                kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if kind not in TYPES:
                    raise ImageRefused(f"not a raster image: {kind or 'no type'}")
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise ImageRefused("image larger than 5 MB")
                return data, TYPES[kind]
        raise ImageRefused("too many redirects")
    finally:
        if own:
            client.close()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
