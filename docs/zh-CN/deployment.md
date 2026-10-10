# 部署

[English](../deployment.md) · **简体中文**

本文说明怎样在一个单位内部把实例开放给其他人使用：经反向代理提供 HTTPS，为不能直接连到服务端的客户端配置 STUN/TURN，
以及开放之前要检查的事项。只在一台机器上用，或同一局域网里的几台设备，[入门指南](getting-started.md#从其他设备访问)
里的 mkcert 做法就够了。

文中的示例都在 Linux 上与当前版本的应用实际跑过：Caddy 2.6、nginx 1.24、coturn 4.6，并用无头 Chromium 经每种代理、
只走 coturn 中继建立过连接。主机名、地址和密码都是示例。

- [1. 哪些东西要能连通](#1-哪些东西要能连通)
- [2. 对外开放前的检查清单](#2-对外开放前的检查清单)
- [3. 反向代理与 HTTPS](#3-反向代理与-https)
- [4. STUN 与 TURN](#4-stun-与-turn)
- [5. 常见问题](#5-常见问题)

## 1. 哪些东西要能连通

一场会议有两类流量：

| 流量                           | 协议与端口                                         | 是否经过反向代理 |
| ------------------------------ | -------------------------------------------------- | ---------------- |
| 网页、HTTP 接口、WebRTC 信令   | HTTPS，代理上的 TCP 443                            | 是               |
| 麦克风音频与数据通道（字幕等） | WebRTC，UDP，直接在浏览器和运行 `serve` 的机器之间 | 否               |

应用的 WebRTC 实现（aiortc）为每个连接在本机**每一块网卡**上各开一个随机 UDP 端口，与 `server.host` 无关，端口范围也不能
配置。所以浏览器必须能向应用所在的机器发 UDP，或者两边都能连到一台在中间转发的 TURN 服务器（§4）。

除此之外浏览器不需要访问任何地方：页面不从别的主机加载脚本，也不向第三方发请求。

## 2. 对外开放前的检查清单

- **访问口令。** 设置 `server.password_env`，在 `.env` 里放一个足够长的口令（[配置说明](configuration.md#server)）。
  放在反向代理后面时应用监听 `127.0.0.1`，`check` 和 `serve` 就不会再提醒没有口令了：照样要设。
- **只用 HTTPS。** 浏览器只在 HTTPS 页面上允许采集麦克风和屏幕，口令和会话 Cookie 也不能明文过网。在代理上把 HTTP
  重定向到 HTTPS。
- **只监听本机。** 代理和应用在同一台机器上时设 `server.host = "127.0.0.1"`，别人就绕不过代理直接访问应用的 HTTP 端口。
- **防火墙。** 代理上开放 TCP 443（以及用于重定向和证书续期的 80）。媒体流量二选一：允许客户端所在网段向应用所在机器的
  临时端口发 UDP（Linux 默认 32768–60999，Windows 为 49152–65535），或者部署 TURN，只开放它的端口（§4.2）。
  不要开放应用的 HTTP 端口（7860）和各推理服务的端口。
- **数据目录。** `session.data_dir`（默认 `data/`）里有会议数据库、截图、任务文件、日志和 `auth_secret`。用单独的系统账号
  运行应用，并让这个目录只有该账号能读（`chmod 700 data`）。`.env` 也一样（`chmod 600 .env`）。
- **TURN 只中继到会议服务器。** 按 §4.2 配置时，coturn 拒绝中继到其他任何地址；这样即使每个已登录的浏览器都能读到 TURN
  凭据，也没法拿它去连别的机器。
- **健康检查与指标接口。** `/healthz`、`/readyz` 和 `/metrics` 不需要登录（[接口说明](interfaces.md#58-健康检查与基础指标)）。
  它们不含会议内容，但 `/metrics` 能看出负载和活动情况。§3 的代理示例只允许监控网段访问它们（示例里用 `10.0.0.0/8` 代表），
  按需要修改或删掉那一段。
- **会议数据发往哪里。** 实时模型部署在另一台机器上时，`check` 和 `serve` 会说明：整场会议的转录（模型识图时还有截图）
  都会发往那里。

## 3. 反向代理与 HTTPS

### 3.1 应用的设置

```toml
[server]
host = "127.0.0.1"   # 只让本机的代理连进来
port = 7860
tls_cert = ""        # TLS 在代理上终结
tls_key = ""
password_env = "AGENTIC_MEETING_PASSWORD"
```

应用只信任来自 `127.0.0.1` 和 `::1` 的 `X-Forwarded-For`、`X-Forwarded-Proto`（uvicorn 的默认值），而这两个它都要用：
协议决定会话 Cookie 带不带 `Secure`，客户端地址是登录限速的计数依据。代理在另一台机器上时，启动应用前在
`FORWARDED_ALLOW_IPS` 里写上代理的地址，例如 `FORWARDED_ALLOW_IPS=10.0.0.5`；否则所有客户端看起来都是代理，一个人输错
几次口令，所有人都要等五分钟才能登录。这种情况下应用必须监听代理能连到的地址，所以要在防火墙上把 7860 端口限制为只有
代理能访问。

### 3.2 Caddy

域名解析到这台机器、80 和 443 端口能从互联网访问时，Caddy 会自动申请并续期证书。它自己会设置 `X-Forwarded-For` 和
`X-Forwarded-Proto`，没有长度的响应（ZIP 导出）收到多少转发多少，默认也不限制请求体大小。

```caddy
# /etc/caddy/Caddyfile
meeting.example.org {
	# 健康检查和指标不需要登录：监控网段以外一律拒绝
	@ops {
		path /healthz /readyz /metrics
		not remote_ip 10.0.0.0/8
	}
	respond @ops 403

	reverse_proxy 127.0.0.1:7860
}
```

内网里没有公网域名时，用单位自己的证书：

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

    # 截图上传最大 4 MB；nginx 默认只放行 1 MB
    client_max_body_size 8m;

    # 健康检查和指标不需要登录：只允许监控所在网段访问
    location ~ ^/(healthz|readyz|metrics)$ {
        allow 10.0.0.0/8;
        deny all;
        proxy_pass http://127.0.0.1:7860;
    }

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # ZIP 导出边打包边发送：不缓冲，长下载不因读超时中断
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

这里用 `$proxy_add_x_forwarded_for` 是安全的：应用从右往左读这个列表，遇到第一个不受信任的地址就停，客户端自己带一个伪造的
请求头也冒充不了别的地址。

### 3.4 代理必须满足的条件

| 条件                               | 原因                                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------- |
| 传 `X-Forwarded-Proto: https`      | 应用认为请求走的是 HTTPS 时，会话 Cookie 才带 `Secure`                                |
| 传带客户端地址的 `X-Forwarded-For` | 登录限速（5 分钟内失败 5 次）按客户端地址计数                                         |
| 请求体至少允许 4 MB                | `POST /api/frames` 上传的截图最大 4 MB                                                |
| 不缓冲、允许长时间的响应           | `GET /api/export/{id}.zip` 边打包边发送；截图多的会议要传一阵子                       |
| 允许几秒钟才返回的请求             | `POST /api/offer` 要等服务端收集完 ICE 候选地址才回答，配了 TURN 时还包括申请中继地址 |

应用不用 WebSocket，不需要 `Upgrade` 相关的请求头。

## 4. STUN 与 TURN

### 4.1 什么时候需要

| 情况                                                 | ICE 服务器                             |
| ---------------------------------------------------- | -------------------------------------- |
| 浏览器和服务端在同一局域网，UDP 不受限               | 不需要（`ice_servers = []`）           |
| 不同网段，之间有路由，UDP 能发到服务端               | 不需要                                 |
| 浏览器连不到服务端的地址（NAT、VPN、云上的私有网络） | TURN                                   |
| 中间的防火墙拦 UDP，只允许向外的 TCP                 | TURN，用 `turns:` 地址（TCP 上的 TLS） |

浏览器在 NAT 后面、但能连到服务端时，通常照样能连上：它先发包，服务端回到它看到的地址。只有浏览器根本发不到服务端时才需要
TURN。浏览器和应用拿到的是同一份 `server.ice_servers`；浏览器每次连接前从 `GET /api/ice` 取（[接口说明](interfaces.md#52-webrtc-信令)）。

### 4.2 coturn

把 coturn 装在浏览器和应用服务端都能连到的机器上，通常就是代理那台。下面的配置只接受一个固定用户，监听 3478（UDP 和 TCP）
与 5349（TLS），并且只中继到会议服务器。

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

# 中继端口范围，防火墙放行同一范围的 UDP
min-port=49160
max-port=49200

# TURN 服务器在 NAT 后面时：公网地址/本机地址
# external-ip=203.0.113.10/192.168.1.20

# 只允许中继到会议服务器，其他地址一律拒绝
denied-peer-ip=0.0.0.0-255.255.255.255
denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
allowed-peer-ip=192.168.1.20

no-cli
no-multicast-peers
```

- `allowed-peer-ip` 填运行 `serve` 的那台机器的地址，也就是 TURN 服务器连它用的地址。那台机器有多个地址时，把浏览器可能拿到的
  每一个都列上，否则中继到其余地址会被拒绝（coturn 日志里是 `403 Forbidden IP`）。
- 密码就写在这个文件里：让它只有 coturn 的账号能读（`chmod 640 /etc/turnserver.conf`，属组 `turnserver`）。
- TURN 所在机器的防火墙：3478 的 UDP 和 TCP、5349 的 TCP，以及 UDP 49160–49200。
- coturn 要能读到证书。Caddy 或 certbot 续期之后重启 coturn，让它用上新证书。

在另一台机器上用 coturn 自带的客户端检查：

```bash
turnutils_uclient -u meeting -w '<密码>' -e <会议服务器地址> -r 3480 <TURN 服务器地址>
```

测试期间在会议服务器上运行 `turnutils_peer -L <会议服务器地址> -p 3480`。配置正确时输出 `Total lost packets 0`；对端地址不在
`allowed-peer-ip` 里时是 `error 403 (Forbidden IP)`，密码不对时是 `Cannot complete Allocation`。

### 4.3 应用的配置

```toml
[server]
ice_servers = [
  { urls = ["turn:turn.example.org:3478", "turns:turn.example.org:5349"], username = "meeting", credential_env = "AGENTIC_MEETING_TURN_PASSWORD" },
]
```

```bash
# .env
AGENTIC_MEETING_TURN_PASSWORD=<与 turnserver.conf 里相同的密码>
```

变量没设置时 `check` 会列出来；配置了 TURN 却没有访问口令时，`check` 和 `serve` 会给出提醒。`turns:` 地址里的主机名必须与
TURN 服务器的证书相符。

### 4.4 检查一次连接

在另一个标签页里打开 `chrome://webrtc-internals`（Chrome、Edge）或 `about:webrtc`（Firefox），开始一场会议，看选中的
候选地址对（selected candidate pair）。本地一侧是 `relay` 表示经过了 TURN；`host` 或 `srflx` 表示直连。想在测试时强制走 TURN，
就在防火墙上拦住客户端到应用所在机器的 UDP。

## 5. 常见问题

**浏览器始终不弹出麦克风授权，或者不能共享屏幕。** 页面不是安全上下文：从别的机器用 `http://` 打开，或者用了证书里没有的
地址。用 `https://` 加证书里的域名打开。

**页面一直显示「连接中…」，然后报「连接失败」。** 如果浏览器开发者工具里 `POST /api/offer` 是成功的，说明信令没问题，是 ICE
没找到能通的路径。

- 没有 TURN：浏览器到应用所在机器的 UDP 被拦了，或者浏览器到服务端的哪个地址都不通。配置 TURN（§4）。
- 有 TURN：运行 `turnutils_uclient`（§4.2）。`401` 或 `Cannot complete Allocation` 表示用户名或密码与 `.env` 里的不一致；
  coturn 日志里的 `403 Forbidden IP` 表示 `allowed-peer-ip` 没有包含服务端给出的地址。改了 `.env` 要重启应用：TURN 密码是
  启动时读的。
- `chrome://webrtc-internals` 列出了尝试过的每一对候选地址以及失败的原因。

**会话 Cookie 没有 `Secure` 标记。** 代理没有传 `X-Forwarded-Proto`，或者应用不信任代理的地址，于是认为请求走的是 HTTP。
加上这个请求头（§3.3）；代理在另一台机器上时再设置 `FORWARDED_ALLOW_IPS`（§3.1）。

**浏览器提示混合内容（mixed content）。** 页面只请求相对地址，出现这个提示说明前面有东西在用 `http://` 提供页面或重定向：
用 `https://` 打开页面，并确认代理没有把 `Location` 响应头改写成 HTTP。

**所有人都被告知登录尝试次数太多。** 所有客户端看起来都是代理的地址，见 §3.1 的 `FORWARDED_ALLOW_IPS`。

**截图上传失败，返回 413。** 代理限制了请求体大小；把 nginx 的 `client_max_body_size` 调到至少 4 MB。

**导出下载到一半就断了。** 代理缓冲了响应或者超时了；见 §3.3 的 `proxy_buffering` 和 `proxy_read_timeout`。
