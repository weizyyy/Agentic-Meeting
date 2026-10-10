# Deployment

**English** · [简体中文](zh-CN/deployment.md)

Use this guide after [getting started](getting-started.md) to expose an instance to other devices.
The examples use `meeting.example.org` for the page and `turn.example.org` for TURN. Replace these
names, documentation-only IP addresses and certificate paths with your own. Choose **one** HTTP
proxy; keep the application and proxy on the same machine for the configuration below.

## 1. Two network paths

```text
Browser -- HTTPS :443 --> Caddy or nginx -- HTTP 127.0.0.1:7860 --> application
Browser <---------------- WebRTC / ICE -----------------------> application
Browser / application <-- TURN listener --> coturn <-- UDP relay --> peer
```

HTTPS carries the page, API requests, screenshot uploads (`POST /api/frames`) and
`POST`/`PATCH /api/offer` signaling. Screen sharing uploads still images over HTTP; it does not
send a WebRTC video track. Audio and the WebRTC data channel use a separate ICE connection. Reverse-proxying the page does not
make media cross a firewall or NAT. An empty ICE list can work on a reachable LAN; use STUN for
address discovery and TURN when direct candidates cannot connect, such as across subnets or
restrictive NAT. Both the browser and application must reach the configured TURN listener.

Browser microphone and screen capture require a secure context and user permission. `localhost`
is an exception for development; `http://<LAN IP>` is not. The certificate must be trusted on every
client, including phones. See the browser requirements for [microphones](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)
and [screen capture](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia).

## 2. Application and proxy trust

Merge this into your existing `[server]` section; keep your model and other configuration intact:

```toml
[server]
host = "127.0.0.1"
port = 7860
tls_cert = ""
tls_key = ""
password_env = "AGENTIC_MEETING_PASSWORD"
```

Set a unique, long access passphrase (at least 8 characters) in the environment named above or the
repository's ignored `.env`. Keep `.env` readable only by the service owner. Build the client
(`cd client && npm ci && npm run build`), run `uv run agentic-meeting check`, then start the
application as described in [getting started](getting-started.md#run). Without `--with-services`,
`serve` starts only the application; inference services must already be available for a meeting.
For example, in the shell or service environment:

```bash
export FORWARDED_ALLOW_IPS=127.0.0.1
uv run agentic-meeting serve
```

The application creates uvicorn programmatically; `--forwarded-allow-ips` is not an
`agentic-meeting` CLI option. Uvicorn trusts forwarded headers only from the configured proxy
addresses ([settings](https://uvicorn.dev/settings/#http)). These examples connect from
`127.0.0.1`; do not set the trust list to `*`. Keep port 7860 unreachable from other machines.
If the proxy is on another host, this loopback setup does not apply: secure the backend connection,
restrict access to that proxy and trust only its actual address.

The proxy must preserve `Host`, cookies and `X-CSRF-Token`, and supply the original HTTPS scheme
and client address. The login cookie is `HttpOnly; SameSite=Strict`, with `Secure` only when the
application sees HTTPS. Confirm `Secure` in the browser after logging in. Use one origin for the
page and `/api`; no separate API hostname, path prefix or permissive CORS setting is needed.
The access password grants access to **all** meeting records; it is not a per-person permission.

## 3. Caddy HTTPS termination

Save as your Caddyfile:

```caddyfile
meeting.example.org {
    reverse_proxy 127.0.0.1:7860 {
        transport http {
            dial_timeout 5s
            response_header_timeout 120s
        }
    }
}
```

Caddy obtains and renews a public certificate and redirects HTTP to HTTPS when DNS resolves to
this server and its certificate challenge can succeed; the normal setup needs inbound TCP 80 and 443. For an internal organization hostname, provision a certificate trusted by your clients and
add `tls /path/to/fullchain.pem /path/to/private-key.pem` inside the site block. An untrusted
internal CA does not satisfy browser capture requirements. See [automatic HTTPS](https://caddyserver.com/docs/automatic-https).

For this HTTP upstream, Caddy preserves `Host`, sets `X-Forwarded-Proto`, `X-Forwarded-Host` and
`X-Forwarded-For`, and ignores spoofed incoming `X-Forwarded-*` values by default. Do not override the
scheme to `http`. The 120-second limit waits for response headers, including offer negotiation;
no response-body read/write deadline or whole-response buffering is configured. Unknown-length
streamed exports are flushed as they arrive. Do not add short total-response or streaming timeouts
that would cut off downloads. These are Caddy's [reverse-proxy behaviors](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

Validate before reloading the installed service:

```bash
caddy validate --config /path/to/Caddyfile --adapter caddyfile
```

## 4. nginx HTTPS termination

Provision and renew a trusted certificate separately. Put these blocks inside nginx's `http`
context (for example, an included site file):

```nginx
server {
    listen 80;
    server_name meeting.example.org;
    return 308 https://meeting.example.org$request_uri;
}

server {
    listen 443 ssl;
    server_name meeting.example.org;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/private-key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        send_timeout 3600s;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        client_max_body_size 5m;
    }
}
```

This is a single, directly reached proxy: overwriting `X-Forwarded-For` prevents clients from
choosing the address used for login rate limiting. A CDN or another proxy requires a separately
configured trust chain. `proxy_pass` has no URI suffix, so `/api/offer` and other paths keep their
methods and bodies. The offer endpoint is HTTP, not a WebSocket; no Upgrade rule is required.
Response buffering and caching are disabled for private, potentially long downloads. The 5 MiB
request limit accommodates the application’s 4 MiB screenshot limit plus multipart overhead.

The read/send limits are idle intervals between I/O operations, not a one-hour meeting or download
limit. Increase them if a valid request can remain idle longer; monitor resource use rather than
silently retrying state-changing offers. See nginx's [proxy directives](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
and [client send timeout](https://nginx.org/en/docs/http/ngx_http_core_module.html#send_timeout).

```bash
nginx -t
```

Only reload after validation succeeds and check that certificate renewal also reloads the service.

## 5. TURN with coturn

This example uses long-term username/password authentication, which the current application
supports. It does not issue expiring TURN REST credentials. Use a dedicated TURN password, separate
from the application password, and rotate it in coturn and the application's environment together;
restart the application because it reads ICE credentials at startup. With authentication enabled,
`GET /api/ice` gives these credentials to **every logged-in client**. Environment storage prevents
committing a secret; it does not hide the TURN password from participants.

### 5.1 Listener, relay and credentials

Install coturn with TLS and SQLite support using your operating system's package or the
[official project](https://github.com/coturn/coturn). Run it as a dedicated service account.
For a TURN host directly assigned a public IPv4 address, use this `turnserver.conf`:

```ini
listening-ip=203.0.113.10
relay-ip=203.0.113.10
listening-port=3478
tls-listening-port=5349
realm=turn.example.org
fingerprint
lt-cred-mech
userdb=/path/to/private/turndb.sqlite
cert=/path/to/turn-fullchain.pem
pkey=/path/to/turn-private-key.pem
min-port=49160
max-port=49200
no-dtls
no-tcp-relay
no-multicast-peers
no-cli
```

`203.0.113.10` is an example only. The TLS certificate must cover `turn.example.org`; the page's
certificate is not automatically a certificate for TURN. `no-tcp-relay` disables TCP **peer relay**,
not the TCP/TLS client listeners. All three client transports below still use UDP relay ports.
The small relay range is a starting point; size it for concurrent allocations and monitor capacity.

Create the protected database directory first. Under the coturn service account, with the same
password that you will supply to the application:

```bash
umask 077
# Set AGENTIC_MEETING_TURN_PASSWORD privately; never paste it into this file.
turnadmin -a -u meeting -r turn.example.org -p "$AGENTIC_MEETING_TURN_PASSWORD" \
    -b /path/to/private/turndb.sqlite
turnserver -c /path/to/turnserver.conf
```

The variable must be set and nonempty before invoking `turnadmin`; coturn does not interpolate
application `*_env` fields. Protect the database, certificate key and logs from other accounts.
Before exposing the listener, restrict relay peers using coturn's `denied-peer-ip` /
`allowed-peer-ip` rules and your network firewall: allow the application's actual media addresses
and the TURN relay address when both endpoints relay, and deny unrelated private networks.
Do not enable `allow-loopback-peers` on a shared deployment. See the official
[configuration example](https://github.com/coturn/coturn/blob/master/examples/etc/turnserver.conf)
and [account administration](https://github.com/coturn/coturn/wiki/turnadmin).

If the TURN host is behind NAT, set `listening-ip` and `relay-ip` to its private interface and add
`external-ip=<public-IP>/<private-IP>`. Forward listener ports and the entire UDP relay range
without changing port numbers. The public address must be reachable from both endpoints;
`external-ip` alone does not configure your router or firewall. A TURN server behind carrier-grade
NAT without inbound port forwarding needs a different reachable host.

### 5.2 Application ICE list

Merge into the same `[server]` section:

```toml
ice_servers = [
  "stun:turn.example.org:3478",
  { urls = ["turn:turn.example.org:3478?transport=udp", "turn:turn.example.org:3478?transport=tcp", "turns:turn.example.org:5349?transport=tcp"], username = "meeting", credential_env = "AGENTIC_MEETING_TURN_PASSWORD" },
]
```

Set `AGENTIC_MEETING_TURN_PASSWORD` in the application's environment or ignored `.env`, then
restart it. This same list is used by the application and browser. UDP is preferred when reachable;
TCP and TLS to TURN offer alternatives when UDP listeners are blocked. The pinned server ICE
implementation uses the first supported TURN URL, so this list does not guarantee server-side
transport failover; put a transport reachable from the application first. The browser can consider
the listed alternatives. Reorder or use a single TURN URL for the application’s network, restart
the application, and verify connectivity from both endpoints. `turns:` is TURN TLS, not
HTTPS and not something an HTTP `reverse_proxy` routes. If clients permit only TCP 443, a dedicated
TURN address with a TLS listener on 443 can be used; update the ICE URL and firewall. It cannot
share the HTTP proxy's same address and port through these examples.

### 5.3 Firewall and verification

| Path                              | Allow                                                                                       |
| --------------------------------- | ------------------------------------------------------------------------------------------- |
| Browser → page proxy              | TCP 443; TCP 80 for redirect / applicable certificate challenges                            |
| Other machines → application HTTP | Block 7860; use the proxy                                                                   |
| Browser and application → TURN    | UDP/TCP 3478, TCP 5349 for the listed URLs                                                  |
| TURN relay ↔ media peers          | UDP 49160–49200 on TURN, plus the actual peer media ports and return traffic                |
| Direct WebRTC, if desired         | Routable ICE candidate UDP ports on the application host; these are separate from port 7860 |

An HTTPS-only firewall rule does not enable media. The application exposes no fixed WebRTC UDP
port-range setting; use TURN or a network policy appropriate for its actual candidates.

From a host on each relevant network, use coturn's
[test client](https://github.com/coturn/coturn/wiki/turnutils_uclient) with your private test
credentials (never post its verbose logs with passwords or meeting addresses):

```bash
turnutils_uclient -y -u meeting -w "$AGENTIC_MEETING_TURN_PASSWORD" -p 3478 turn.example.org
turnutils_uclient -y -t -u meeting -w "$AGENTIC_MEETING_TURN_PASSWORD" -p 3478 turn.example.org
openssl s_client -connect turn.example.org:5349 -servername turn.example.org \
    -CAfile /path/to/trusted-ca.pem -verify_hostname turn.example.org \
    -verify_return_error </dev/null
turnutils_uclient -y -t -S \
    -u meeting -w "$AGENTIC_MEETING_TURN_PASSWORD" -p 5349 turn.example.org
```

The `turnutils_uclient` commands allocate two relay endpoints on TURN and exchange packets; permit
its relay address in the peer ACL for this check. Expect successful authenticated allocations,
received packets and no unexplained loss; a wrong password must fail. In the [coturn 4.7.0 test client](https://github.com/coturn/coturn/blob/4.7.0/src/apps/uclient/mainuclient.c),
`turnutils_uclient -E` alone does **not** enable certificate verification. Use the separate
[OpenSSL check](https://docs.openssl.org/3.6/man1/openssl-s_client/) for the CA chain and hostname; it must succeed with the correct CA/name and fail with an
untrusted CA or wrong name. A successful encrypted relay alone does not prove server identity.
Then connect the real page from the target network,
check the selected candidate pair in browser WebRTC diagnostics, and verify audio, captions and
screen sharing. A successful TURN tool run alone does not prove browser connectivity across NAT.

## 6. Before exposing the instance

- Enable the access password; confirm unauthenticated `GET /api/ice` and meeting APIs return 401.
- Use trusted HTTPS on every device, HTTP-to-HTTPS redirect, and a login cookie with `Secure`.
- Keep backend HTTP and inference/admin ports private; open only the chosen HTTP and media paths.
- Protect `.env`, `data/` (including `auth_secret`, recordings, screenshots and task artifacts),
  TURN database and private keys with service-owner permissions; protect backups too. Keep `data/`
  outside the proxy's static document root. A login does not protect files exposed by another service.
- Check DNS, certificate renewal, disk space, TURN reachability and credential rotation. All users
  share record access; do not expose this instance to people who must not see its meetings.
- Test a login, a new meeting, a resumed meeting, a long export and a task artifact download from
  the intended client network. Avoid proxy caches and logs containing credentials or meeting content.

For HTTP reachability, `curl --fail https://meeting.example.org/api/auth` should return the
password-enabled status. This tests the web endpoint, not inference readiness. Use
`uv run agentic-meeting services status` for configured inference services.

## 7. Troubleshooting

| Symptom                                         | Check                                                                                                                                                                                                                                                           |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No microphone prompt or capture API unavailable | Use HTTPS with a trusted, hostname-matching certificate; check `window.isSecureContext`, browser/site permissions and OS microphone permissions. Retry after allowing access. Screen capture also needs a user gesture and browser support.                     |
| Page loads but ICE fails / no audio             | Inspect browser WebRTC diagnostics and TURN logs; confirm `/api/ice` succeeds after login, credentials match, DNS and listener ports work from both endpoints, and relay ports/peer ACL/NAT mapping allow traffic. A successful `/api/offer` is only signaling. |
| Mixed-content error                             | Serve the entire page and API from the same HTTPS origin; remove `http://` API URLs or assets and use these whole-site proxies. The proxy's private loopback HTTP hop is not browser mixed content.                                                             |
| Certificate warning / `turns:` fails            | Check DNS names, certificate chain, expiry and client trust for the page and TURN separately. Install the organization CA on each client where required; do not disable TLS verification.                                                                       |
| Login repeats / cookie lacks `Secure`           | Verify the proxy sends `X-Forwarded-Proto: https`, uvicorn trusts only its real peer address, and the browser uses one hostname. Restart after changing password environment values.                                                                            |
| API returns 401 or 403                          | 401 means missing/expired login; log in again. For POST/PATCH/DELETE, 403 can mean missing or stale `X-CSRF-Token`; the web client sends it, custom clients must use the token returned by `/api/auth`. Keep cookies and the header through the proxy.          |
| Login returns 429 for many users                | Wait for the five-minute window; confirm trusted forwarded client addresses are distinct rather than all appearing as the proxy. Do not trust arbitrary incoming headers.                                                                                       |
| Offer or download gets 502/504 / truncates      | Confirm loopback backend reachability, inspect proxy/app logs, check header/idle timeouts and buffering, and repeat a slow export. An hour-long meeting does not require keeping an offer HTTP response open for an hour.                                       |

See [troubleshooting](troubleshooting.md) for capture levels and inference failures.

## 8. Validation scope

Validated with Caddy **2.11.7**, nginx **1.28.0** and coturn **4.7.0**, using local-only listeners,
a temporary trusted CA and generated credentials. The configuration blocks above were used with
fixture addresses, ports and certificate/state paths substituted, plus test-only process settings.
Caddy used a fixed certificate with automatic HTTPS disabled; its automatic certificate/redirect
path was not tested. Only the isolated TURN fixture permitted loopback peers; the deployment
example must keep them blocked.

- Both HTTP proxies passed real TLS, application login and `Secure` cookie checks, unauthenticated
  and missing-CSRF rejection, `POST`/`PATCH /api/offer` forwarding, `/api/ice` and a complete 5 MiB
  chunked download; nginx also forwarded a 2 MiB multipart upload. The application used a fake
  media handler to avoid starting inference.
- Coturn passed authenticated allocations and bidirectional relay data over UDP, TCP and TLS
  client transports; wrong credentials were rejected on each transport. Separate OpenSSL checks
  accepted the matching CA/hostname and rejected an untrusted CA or wrong hostname.
  The pinned server ICE gatherer also produced a relay candidate for each transport tested
  separately; host address discovery was restricted to loopback for this check.

These are protocol checks. Real browser microphone/screen capture, public NAT traversal,
production DNS, certificate issuance/renewal, concurrent capacity and production firewall/peer ACL
rules still require verification on the deployment's own networks; no model or GPU was used.
