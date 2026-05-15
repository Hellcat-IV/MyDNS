#!/usr/bin/env python3
"""Local DNS server — SQLite backend, in-memory cache, upstream failover."""
import logging
import os
import socket
import sqlite3
import threading
import time

from dnslib import (
    QTYPE, RCODE, RR,
    A, AAAA, CNAME, MX, PTR, TXT,
    DNSRecord,
)
from dnslib.server import BaseResolver, DNSServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DNS] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/dns.db")
UPSTREAM = os.environ.get("UPSTREAM_DNS", "1.1.1.1 8.8.8.8").split()
UPSTREAM_TIMEOUT = 2.0


def _build_rr(name: str, rtype: str, value: str, ttl: int, priority: int):
    if not name.endswith("."):
        name += "."
    try:
        if rtype == "A":
            return RR(name, QTYPE.A, rdata=A(value), ttl=ttl)
        if rtype == "AAAA":
            return RR(name, QTYPE.AAAA, rdata=AAAA(value), ttl=ttl)
        if rtype == "CNAME":
            t = value if value.endswith(".") else value + "."
            return RR(name, QTYPE.CNAME, rdata=CNAME(t), ttl=ttl)
        if rtype == "MX":
            t = value if value.endswith(".") else value + "."
            return RR(name, QTYPE.MX, rdata=MX(t, priority), ttl=ttl)
        if rtype == "TXT":
            return RR(name, QTYPE.TXT, rdata=TXT(value), ttl=ttl)
        if rtype == "PTR":
            t = value if value.endswith(".") else value + "."
            return RR(name, QTYPE.PTR, rdata=PTR(t), ttl=ttl)
    except Exception as exc:
        log.warning("Bad record %s %s %s: %s", name, rtype, value, exc)
    return None


def _load_cache(db_path: str) -> dict:
    cache: dict[tuple[str, int], list[RR]] = {}
    if not os.path.exists(db_path):
        return cache
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, type, value, ttl, priority FROM dns_records WHERE enabled=1"
        ).fetchall()
        conn.close()
        for row in rows:
            name = row["name"].lower()
            if not name.endswith("."):
                name += "."
            qtype_int = getattr(QTYPE, row["type"], None)
            if qtype_int is None:
                continue
            rr = _build_rr(name, row["type"], row["value"], row["ttl"], row["priority"])
            if rr:
                cache.setdefault((name, qtype_int), []).append(rr)
    except Exception as exc:
        log.error("Cache load failed: %s", exc)
    log.info("Cache loaded: %d record set(s)", len(cache))
    return cache


def _forward(request: DNSRecord, upstreams: list[str], timeout: float):
    data = request.pack()
    for server in upstreams:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(data, (server, 53))
            resp, _ = sock.recvfrom(4096)
            sock.close()
            return DNSRecord.parse(resp)
        except Exception as exc:
            log.warning("Upstream %s: %s", server, exc)
    return None


class LocalResolver(BaseResolver):
    def __init__(self, db_path: str, upstreams: list[str]):
        self._db_path = db_path
        self._upstreams = upstreams
        self._lock = threading.RLock()
        self._cache: dict = _load_cache(db_path)

    def reload(self):
        new = _load_cache(self._db_path)
        with self._lock:
            self._cache = new

    def resolve(self, request: DNSRecord, handler) -> DNSRecord:
        qname = str(request.q.qname).lower()
        if not qname.endswith("."):
            qname += "."
        qtype = request.q.qtype

        with self._lock:
            hits = self._cache.get((qname, qtype), [])
            # Also check CNAME at the queried name when type is not CNAME
            cname_hits = self._cache.get((qname, QTYPE.CNAME), []) if qtype != QTYPE.CNAME else []

        if hits or cname_hits:
            reply = request.reply()
            for rr in cname_hits:
                reply.add_answer(rr)
                # Follow CNAME target locally if present
                target = str(rr.rdata.label).lower()
                with self._lock:
                    extra = self._cache.get((target, qtype), [])
                for xrr in extra:
                    reply.add_answer(xrr)
            for rr in hits:
                reply.add_answer(rr)
            log.info("LOCAL   %s %s → %d rr(s)", qname, QTYPE.get(qtype), len(reply.rr))
            return reply

        # Upstream failover
        response = _forward(request, self._upstreams, UPSTREAM_TIMEOUT)
        if response:
            log.info("FORWARD %s %s", qname, QTYPE.get(qtype))
            return response

        log.warning("SERVFAIL %s %s", qname, QTYPE.get(qtype))
        reply = request.reply()
        reply.header.rcode = RCODE.SERVFAIL
        return reply


class _Watchdog(threading.Thread):
    def __init__(self, db_path: str, resolver: LocalResolver, interval: float = 1.0):
        super().__init__(daemon=True)
        self._db_path = db_path
        self._resolver = resolver
        self._interval = interval
        try:
            self._last_mtime = os.path.getmtime(db_path)
        except FileNotFoundError:
            self._last_mtime = 0.0

    def run(self):
        while True:
            time.sleep(self._interval)
            try:
                mtime = os.path.getmtime(self._db_path)
                if mtime != self._last_mtime:
                    self._last_mtime = mtime
                    log.info("DB changed, reloading cache...")
                    self._resolver.reload()
            except FileNotFoundError:
                pass
            except Exception as exc:
                log.error("Watchdog: %s", exc)


def _wait_for_db(db_path: str, max_wait: int = 30):
    for i in range(max_wait):
        if os.path.exists(db_path):
            return
        log.info("Waiting for DB (%ds)...", i + 1)
        time.sleep(1)
    log.warning("DB not found after %ds, starting with empty cache", max_wait)


def main():
    _wait_for_db(DB_PATH)
    resolver = LocalResolver(DB_PATH, UPSTREAM)

    _Watchdog(DB_PATH, resolver).start()

    udp = DNSServer(resolver, port=53, address="0.0.0.0", tcp=False)
    tcp = DNSServer(resolver, port=53, address="0.0.0.0", tcp=True)
    udp.start_thread()
    tcp.start_thread()

    log.info("DNS listening on 0.0.0.0:53 (UDP+TCP)")
    log.info("Upstream resolvers: %s", ", ".join(UPSTREAM))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        udp.stop()
        tcp.stop()


if __name__ == "__main__":
    main()
