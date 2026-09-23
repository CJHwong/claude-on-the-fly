# Manage network egress

Under sandboxing, model endpoints in the packaged allowlist work automatically.
Unknown public HTTPS hosts pause for approval; permanently blocked hosts are refused.

Pre-authorize a host only when repeated prompts are undesirable:

```yaml
egress:
  allow:
    - pypi.org
    - files.pythonhosted.org
```

Block a host without offering approval:

```yaml
egress:
  never_ask:
    - telemetry.example.com
```

Allowing a hostname does not permit it to resolve to a private or loopback address.
For a deliberately local development service, opt in separately:

```yaml
egress:
  private_allow:
    - host.docker.internal
```

Both normal lists and `private_allow` reload on the next CONNECT and are unioned with
packaged policy. Entries are hostnames or IP addresses, not URLs or wildcard patterns.
An IPv6 address is written bare in the list (`::1`) and bracketed in a URL
(`https://[::1]:8443`); a zone id such as `%eth0` is not accepted. Every hostname is
resolved and pinned before connection; private/loopback answers require the explicit
second list. Link-local addresses, the cloud metadata range included, stay refused even
when listed.

An entry that is not a hostname or an address is dropped with an error naming it, and
the rest of the list still applies. A list that is not a list at all is ignored with an
error, and packaged policy remains active.

Approving or allowing a host permits an opaque TLS tunnel. The proxy learns the
destination, not the request body, so every allowed host is a possible data channel.
