# Deployment

**English** · [简体中文](zh-CN/deployment.md)

This guide covers making an instance available to other people inside an organization: HTTPS
through a reverse proxy, STUN and TURN for clients that cannot reach the server directly, and what
to check before opening it up. For a single machine, or a few devices on one LAN, the mkcert setup
in [getting started](getting-started.md#access-from-other-devices) is enough.

The examples were run against this version of the application with Caddy 2.6, nginx 1.24 and
coturn 4.6 on Linux. A headless Chromium connected through each proxy with relay-only ICE through
coturn. Host names, addresses and passwords below are placeholders.

- [1. What has to be reachable](#1-what-has-to-be-reachable)
- [2. Checklist before exposing an instance](#2-checklist-before-exposing-an-instance)
- [3. Reverse proxy and HTTPS](#3-reverse-proxy-and-https)
- [4. STUN and TURN](#4-stun-and-turn)
- [5. Troubleshooting](#5-troubleshooting)

## 1. What has to be reachable

A meeting uses two kinds of traffic:

| Traffic                                         | Protocol and ports                                                   | Goes through the reverse proxy |
| ----------------------------------------------- | -------------------------------------------------------------------- | ------------------------------ |
| Web page, HTTP API, WebRTC signaling            | HTTPS, TCP 443 on the proxy                                          | Yes                            |
| Microphone audio and data channel (captions, …) | WebRTC over UDP, between the browser and the machine running `serve` | No                             |

The application's WebRTC stack (aiortc) listens on a random UDP port for each connection, on every
network interface of the machine, whatever `server.host` says. The port range cannot be configured.
The browser must therefore be able to send UDP to the application machine, or both must be able to
reach a TURN server that relays between them (§4).

The browser needs nothing else: the page loads no scripts from other hosts and makes no requests to
third parties.

## 2. Checklist before exposing an instance

- **Access password.** Set `server.password_env` and put a long passphrase in `.env`
  ([configuration](configuration.md#server)). Behind a reverse proxy the application listens on
  `127.0.0.1`, so `check` and `serve` no longer warn about a missing password: set it anyway.
- **HTTPS only.** Browsers allow the microphone and screen capture only on HTTPS pages, and the
  password and session cookie must not cross the network in clear text. Redirect HTTP to HTTPS on the
  proxy.
- **Bind to loopback.** With a proxy on the same machine, set `server.host = "127.0.0.1"` so that the
  application's HTTP port is not reachable around the proxy.
- **Firewall.** Open TCP 443 (and 80 for the redirect and certificate renewal) on the proxy. For
  media, either allow UDP from the client networks to the application machine's ephemeral ports
  (32768–60999 on Linux by default, 49152–65535 on Windows), or run TURN and open only its ports (§4.2).
  Do not open the application's HTTP port (7860) or the ports of the inference services.
- **Data directory.** `session.data_dir` (default `data/`) holds the meeting database, screenshots,
  task files, logs and `auth_secret`. Run the application under its own user account and make the
  directory readable by that user only (`chmod 700 data`). The same goes for `.env` (`chmod 600 .env`).
- **TURN relays only to the meeting server.** Configured as in §4.2, coturn refuses to relay to any
  other address, so its credentials, which every logged-in browser can read, cannot be used to reach
  other machines.
- **Where meeting data goes.** If the realtime LLM runs on another machine, `check` and `serve` say
  so: the transcript (and, with vision, screenshots) is sent there for the whole meeting.

## 3. Reverse proxy and HTTPS

### 3.1 Application settings

```toml
[server]
host = "127.0.0.1"   # only the proxy on this machine connects
port = 7860
tls_cert = ""        # TLS ends at the proxy
tls_key = ""
password_env = "AGENTIC_MEETING_PASSWORD"
```

The application trusts `X-Forwarded-For` and `X-Forwarded-Proto` only from `127.0.0.1` and `::1`
(uvicorn's default). It needs both: the protocol decides whether the session cookie is marked
`Secure`, and the client address is what the login rate limit counts. If the proxy runs on another
machine, start the application with that machine's address in `FORWARDED_ALLOW_IPS`, for example
`FORWARDED_ALLOW_IPS=10.0.0.5`; otherwise every client appears as the proxy and one person's typing
mistakes lock everybody out of the login for five minutes. In that case the application has to listen
on an address the proxy can reach, so restrict port 7860 to the proxy in the firewall.

### 3.2 Caddy

Caddy obtains and renews a certificate automatically when the name resolves to the machine and ports
80 and 443 are reachable from the internet. It sets `X-Forwarded-For` and `X-Forwarded-Proto` itself,
streams responses without a length (ZIP exports) as they come, and has no request size limit by
default.

```caddy
# /etc/caddy/Caddyfile
meeting.example.org {
	reverse_proxy 127.0.0.1:7860
}
```

On an internal network without a public name, use your organization's certificate:

```caddy
meeting.example.internal {
	tls /etc/ssl/meeting/fullchain.pem /etc/ssl/meeting/privkey.pem
	reverse_proxy 127.0.0.1:7860
}
```

### 3.3 nginx

```nginx
# /etc/nginx/conf.d/meeting.conf
server {
    listen 80;
    server_name meeting.example.org;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name meeting.example.org;

    ssl_certificate     /etc/ssl/meeting/fullchain.pem;
    ssl_certificate_key /etc/ssl/meeting/privkey.pem;

    # Screenshots are uploaded up to 4 MB; nginx accepts 1 MB by default
    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # ZIP exports are written while they are packed: pass them through unbuffered
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

`$proxy_add_x_forwarded_for` is safe here: the application reads the list from the right and stops at
the first address it does not trust, so a client cannot spoof its address by sending its own header.

### 3.4 What the proxy has to pass

| Requirement                        | Why                                                                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `X-Forwarded-Proto: https`         | The session cookie is marked `Secure` only when the request is seen as HTTPS                                        |
| `X-Forwarded-For` with the client  | The login rate limit (5 failures in 5 minutes) is counted per client address                                        |
| Request bodies of at least 4 MB    | `POST /api/frames` uploads screenshots of up to 4 MB                                                                |
| Unbuffered, long responses         | `GET /api/export/{id}.zip` is streamed while it is packed; large meetings with many screenshots take a while        |
| Requests that take several seconds | `POST /api/offer` answers after the server has gathered its ICE candidates, including TURN allocation if configured |

The application uses no WebSockets; no `Upgrade` headers are needed.

## 4. STUN and TURN

### 4.1 When you need it

| Situation                                                                                | ICE servers                             |
| ---------------------------------------------------------------------------------------- | --------------------------------------- |
| Browser and server on the same LAN, UDP allowed                                          | None (`ice_servers = []`)               |
| Different subnets with routing between them and UDP allowed to the server                | None                                    |
| The server's address is not reachable from the browser (NAT, VPN, cloud private network) | TURN                                    |
| A firewall between them blocks UDP and allows only outgoing TCP                          | TURN with a `turns:` URL (TLS over TCP) |

A browser behind NAT can usually still connect to a server it can reach: it sends first, and the
server answers to the address it sees. TURN is needed when the browser cannot send to the server at
all. The browser and the application both receive `server.ice_servers`; the browser fetches it from
`GET /api/ice` before each connection ([interfaces](interfaces.md#52-webrtc-signaling)).

### 4.2 coturn

Install coturn on a machine that both the browsers and the application server can reach, often the
proxy machine. The configuration below accepts one long-term user, listens on 3478 (UDP and TCP) and
5349 (TLS), and relays only to the meeting server.

```ini
# /etc/turnserver.conf
listening-port=3478
tls-listening-port=5349
realm=meeting.example.org
fingerprint
lt-cred-mech
user=meeting:CHANGE-ME-long-random-password
cert=/etc/ssl/meeting/fullchain.pem
pkey=/etc/ssl/meeting/privkey.pem

# Relay ports; open the same UDP range in the firewall
min-port=49160
max-port=49200

# When the TURN server is behind NAT: public address/local address
# external-ip=203.0.113.10/192.168.1.20

# Relay only to the meeting server, refuse every other address
denied-peer-ip=0.0.0.0-255.255.255.255
denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
allowed-peer-ip=192.168.1.20

no-cli
no-multicast-peers
```

- `allowed-peer-ip` is the address of the machine that runs `serve`, as the TURN server reaches it.
  If that machine has several addresses, list each one the browser could be offered, or relayed
  connections to the others are refused (`403 Forbidden IP` in the coturn log).
- The password is stored in this file: make it readable by the coturn user only
  (`chmod 640 /etc/turnserver.conf`, group `turnserver`).
- Firewall on the TURN machine: 3478 UDP and TCP, 5349 TCP, and UDP 49160–49200.
- coturn needs to read the certificate. When Caddy or certbot renews it, restart coturn so that it
  picks up the new file.

Check it with the client that comes with coturn, from another machine:

```bash
turnutils_uclient -u meeting -w '<password>' -e <meeting server address> -r 3480 <TURN server address>
```

Run `turnutils_peer -L <meeting server address> -p 3480` on the meeting server while testing. A
working setup reports `Total lost packets 0`; an address outside `allowed-peer-ip` reports
`error 403 (Forbidden IP)`, and a wrong password `Cannot complete Allocation`.

### 4.3 Application configuration

```toml
[server]
ice_servers = [
  { urls = ["turn:turn.example.org:3478", "turns:turn.example.org:5349"], username = "meeting", credential_env = "AGENTIC_MEETING_TURN_PASSWORD" },
]
```

```bash
# .env
AGENTIC_MEETING_TURN_PASSWORD=<the same password as in turnserver.conf>
```

`check` reports the variable if it is unset, and `check` and `serve` warn when TURN is configured
without an access password. A `turns:` URL needs a name that matches the TURN server's certificate.

### 4.4 Checking a connection

Open `chrome://webrtc-internals` (Chrome, Edge) or `about:webrtc` (Firefox) in a second tab, start a
meeting, and look at the selected candidate pair. `relay` on the local side means the connection goes
through TURN; `host` or `srflx` means it is direct. To force TURN while testing, block UDP from the
client to the application machine.

## 5. Troubleshooting

**The browser never asks for the microphone, or screen sharing is unavailable.** The page is not a
secure context: it was opened over `http://` from another machine, or through an address that the
certificate does not cover. Open it over `https://` with the name in the certificate.

**The page stays at "连接中…" and then reports "连接失败".** If the browser's developer tools show
that `POST /api/offer` succeeded, signaling worked and ICE did not find a path.

- Without TURN: UDP from the browser to the application machine is blocked, or the browser cannot
  route to any of the server's addresses. Configure TURN (§4).
- With TURN: run `turnutils_uclient` (§4.2). `401` or `Cannot complete Allocation` means the user name
  or password does not match `.env`; `403 Forbidden IP` in the coturn log means `allowed-peer-ip`
  does not include the address the server offered. Restart the application after changing `.env`:
  the TURN password is read at startup.
- `chrome://webrtc-internals` lists every candidate pair that was tried and why it failed.

**The session cookie is not marked `Secure`.** The proxy does not send `X-Forwarded-Proto`, or the
application does not trust the proxy's address, so the application thinks the request came over
HTTP. Set the header (§3.3), and `FORWARDED_ALLOW_IPS` when the proxy runs on another machine
(§3.1).

**Mixed content warnings.** The page requests only relative addresses, so a warning means something
in front of it serves or redirects to `http://`: open the page over `https://` and check that the proxy
does not rewrite `Location` headers to HTTP.

**Everybody is told there were too many login attempts.** All clients appear with the proxy's
address; see `FORWARDED_ALLOW_IPS` in §3.1.

**Screenshots fail with 413.** The proxy limits request bodies; raise `client_max_body_size` (nginx)
to at least 4 MB.

**Exports stop in the middle.** The proxy buffers the response or times out; see `proxy_buffering`
and `proxy_read_timeout` in §3.3.
