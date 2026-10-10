# 部署指南

[English](../deployment.md) · **简体中文**

完成[入门指南](getting-started.md)后，按本文让其他设备访问实例。示例网页域名是
`meeting.example.org`，TURN 域名是 `turn.example.org`；请换成自己的域名、地址和证书路径。
示例 IP 均为文档保留地址，不能原样部署。下述配置假定应用与 HTTP 代理同机运行，Caddy 和 nginx
**选一种**即可。

## 1. 两条网络路径

```text
浏览器 -- HTTPS :443 --> Caddy 或 nginx -- HTTP 127.0.0.1:7860 --> 应用
浏览器 <---------------- WebRTC / ICE -----------------------> 应用
浏览器 / 应用 <-- TURN 监听端口 --> coturn <-- UDP 中继 --> 对端
```

HTTPS 承载网页、API、截图上传（`POST /api/frames`）和 `POST`/`PATCH /api/offer` 信令。
屏幕共享通过 HTTP 上传静态截图，不发送 WebRTC 视频轨；音频及 WebRTC 数据通道使用独立的 ICE 连接。代理网页不会让媒体自动穿过防火墙或 NAT。地址可互通的局域网可能不需要 ICE
服务器；STUN 用于发现地址，跨网段或受限 NAT 下直连失败时需要 TURN。浏览器和应用都必须能访问
配置的 TURN 监听端口。

浏览器采集麦克风和屏幕需要安全上下文及用户许可。开发时 `localhost` 是例外，`http://<局域网 IP>`
不是。每台客户端（包括手机）都要信任证书。浏览器要求见[麦克风](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)
和[屏幕采集](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia)。

## 2. 应用配置与代理信任

合并到现有的 `[server]` 小节，保留其他配置及模型设置：

```toml
[server]
host = "127.0.0.1"
port = 7860
tls_cert = ""
tls_key = ""
password_env = "AGENTIC_MEETING_PASSWORD"
```

在对应环境变量或仓库中忽略的 `.env` 内设置独立的长口令（至少 8 个字符），仅服务运行账号可读
`.env`。构建客户端（`cd client && npm ci && npm run build`），运行 `uv run agentic-meeting check`，
再按[入门指南](getting-started.md#运行)启动应用。不带 `--with-services` 的 `serve` 只启动应用，
开会前推理服务须已可用。例如在 shell 或服务环境中：

```bash
export FORWARDED_ALLOW_IPS=127.0.0.1
uv run agentic-meeting serve
```

应用直接构造 uvicorn，`agentic-meeting` 命令行不支持 `--forwarded-allow-ips`。
Uvicorn 只信任指定代理地址的转发头（[设置说明](https://uvicorn.dev/settings/#http)）。
这些示例的代理通过 `127.0.0.1` 连接；不要将信任列表设为 `*`，不要让其他机器访问 7860。
代理若在另一台主机，这套 loopback 配置不适用：需要保护后端连接、只允许该代理访问，且仅信任
它的实际地址。

代理须保留 `Host`、Cookie、`X-CSRF-Token`，传递原始 HTTPS 协议和客户端地址。登录 Cookie
带 `HttpOnly; SameSite=Strict`，只有应用识别到 HTTPS 时才带 `Secure`；登录后请在浏览器确认。
网页与 `/api` 使用同一源，无需另设 API 域名、路径前缀或宽松 CORS。访问口令授予**所有会议记录**
的访问权，尚不是按个人划分的权限。

## 3. Caddy 终止 HTTPS

保存为 Caddyfile：

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

域名 DNS 指向服务器且证书验证可成功时，Caddy 自动申请、续期公共证书并将 HTTP 重定向到 HTTPS；
通常需要入站 TCP 80 和 443。组织内域名可自行提供客户端信任的证书，在站点块内加入
`tls /path/to/fullchain.pem /path/to/private-key.pem`。客户端不信任的内部 CA 不满足浏览器采集要求。
详见[自动 HTTPS](https://caddyserver.com/docs/automatic-https)。

此 HTTP 上游配置保留 `Host`，Caddy 设置 `X-Forwarded-Proto`、`X-Forwarded-Host` 和
`X-Forwarded-For`，默认忽略客户端伪造的 `X-Forwarded-*` 头。不要把协议覆盖为 `http`。120 秒限制的是等待
响应头（包括 offer 协商）的时间；没有设置响应体读写期限或整份响应缓冲。长度未知的流式导出随写随发，
不要新增会截断下载的短总时限或流时限。详见 Caddy 的[反向代理行为](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。

重新加载已安装的服务前先校验：

```bash
caddy validate --config /path/to/Caddyfile --adapter caddyfile
```

## 4. nginx 终止 HTTPS

单独申请并续期可信证书。将这两个块放入 nginx 的 `http` 上下文（例如其 include 的站点文件）：

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

示例只有一层、由客户端直接访问的代理；覆盖 `X-Forwarded-For` 可阻止客户端伪造登录限速使用的
地址。若前面还有 CDN 或代理，须另外配置可信链。`proxy_pass` 无 URI 后缀，因此 `/api/offer`
等路径保留原请求方法和请求体。offer 是 HTTP 接口，不是 WebSocket，无需 Upgrade 规则。
关闭响应缓冲和缓存，以支持私有记录的长下载。5 MiB 请求上限容纳应用的 4 MiB 截图限制及
表单封装开销。

读写期限是两次 I/O 间的空闲时间，不是会议或下载最多一小时。合法请求若可能更久无数据，请调整
限制并监控资源，不要自动重试会修改状态的 offer。详见 nginx 的[代理指令](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
及[客户端发送期限](https://nginx.org/en/docs/http/ngx_http_core_module.html#send_timeout)。

```bash
nginx -t
```

校验通过再重新加载，并确认续期证书后也会重新加载服务。

## 5. 用 coturn 提供 TURN

示例采用当前应用支持的长期用户名/密码认证，应用不会签发有期限的 TURN REST 凭据。
TURN 密码须与应用访问口令不同；轮换时同步更新 coturn 和应用环境，并重启应用，它在启动时读取
ICE 凭据。启用访问口令后，`GET /api/ice` 仍会把 TURN 凭据交给**每位已登录客户端**。
环境变量避免把秘密提交到仓库，不会对参会者隐藏 TURN 密码。

### 5.1 监听、中继与凭据

使用操作系统软件包或[官方项目](https://github.com/coturn/coturn)安装带 TLS 和 SQLite 支持的 coturn，
以专用服务账号运行。TURN 主机直接拥有公网 IPv4 地址时，使用下面的 `turnserver.conf`：

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

`203.0.113.10` 仅为示例。TLS 证书须覆盖 `turn.example.org`，网页证书不会自动成为 TURN 证书。
`no-tcp-relay` 关闭的是 TCP **对端中继**，不关闭客户端 TCP/TLS 监听；下述三种客户端传输仍使用
UDP 中继端口。小范围中继端口只是起点，需要按并发分配数扩容并监测。

先创建私有数据库目录。使用 coturn 服务账号，以及将要交给应用的同一个密码：

```bash
umask 077
# 私下设置 AGENTIC_MEETING_TURN_PASSWORD，不要在本文件填写密码。
turnadmin -a -u meeting -r turn.example.org -p "$AGENTIC_MEETING_TURN_PASSWORD" \
    -b /path/to/private/turndb.sqlite
turnserver -c /path/to/turnserver.conf
```

调用 `turnadmin` 前必须设置非空变量；coturn 不会解析应用的 `*_env` 字段。数据库、证书私钥和
日志不得让其他账号读取。开放监听端口前，用 coturn 的 `denied-peer-ip` / `allowed-peer-ip`
及网络防火墙限制中继对端：允许应用的实际媒体地址、两端都中继时使用的 TURN 中继地址，阻止无关
内网。共享部署不要启用 `allow-loopback-peers`。详见官方[配置示例](https://github.com/coturn/coturn/blob/master/examples/etc/turnserver.conf)
和[账号管理](https://github.com/coturn/coturn/wiki/turnadmin)。

TURN 主机在 NAT 后时，将 `listening-ip`、`relay-ip` 设为私网网卡地址，再加
`external-ip=<公网 IP>/<私网 IP>`。映射监听端口及整个 UDP 中继范围，内外端口号必须相同。
公网地址须两端均可访问，`external-ip` 不会替你配置路由器或防火墙。运营商 NAT 若不支持入站
端口映射，应换成可访问的 TURN 主机。

### 5.2 应用 ICE 列表

合并到同一 `[server]` 小节：

```toml
ice_servers = [
  "stun:turn.example.org:3478",
  { urls = ["turn:turn.example.org:3478?transport=udp", "turn:turn.example.org:3478?transport=tcp", "turns:turn.example.org:5349?transport=tcp"], username = "meeting", credential_env = "AGENTIC_MEETING_TURN_PASSWORD" },
]
```

在应用环境或忽略的 `.env` 中设置 `AGENTIC_MEETING_TURN_PASSWORD` 后重启。
应用与浏览器使用同一列表。UDP 可达时优先采用；客户端到 TURN 的 TCP/TLS 可为 UDP 监听被封的
网络提供选择。锁定的服务端 ICE 实现只使用第一个受支持的 TURN URL，不能保证服务端传输自动回退；
将应用能访问的传输放在前面，浏览器可考虑列表中的其他选择。按应用网络重排或只选一个 TURN URL，
重启应用并从两端验证连通。`turns:` 是 TURN TLS，不是 HTTPS，不能由 HTTP `reverse_proxy` 转发。
客户端若只允许 TCP 443，可在专用 TURN 地址上用 443 作为 TLS 监听端口，同步更新 ICE URL 和
防火墙；本示例不能让它与 HTTP 代理共用同一地址和端口。

### 5.3 防火墙与验证

| 路径                 | 放行                                                      |
| -------------------- | --------------------------------------------------------- |
| 浏览器 → 网页代理    | TCP 443；TCP 80 用于重定向或适用的证书验证                |
| 其他机器 → 应用 HTTP | 阻止 7860，统一走代理                                     |
| 浏览器及应用 → TURN  | 示例 URL 使用 UDP/TCP 3478、TCP 5349                      |
| TURN 中继 ↔ 媒体对端 | TURN 上的 UDP 49160–49200，以及实际对端媒体端口与返回流量 |
| 需要 WebRTC 直连时   | 应用主机可路由的 ICE 候选 UDP 端口，与 7860 不同          |

只放行 HTTPS 不会让媒体可用。应用没有固定 WebRTC UDP 端口范围设置，需要 TURN 或适合实际
候选地址的网络策略。

从每个相关网络，用 coturn 的[测试客户端](https://github.com/coturn/coturn/wiki/turnutils_uclient)
和私有测试凭据运行（不要公开含密码或会议地址的详细日志）：

```bash
turnutils_uclient -y -u meeting -w "$AGENTIC_MEETING_TURN_PASSWORD" -p 3478 turn.example.org
turnutils_uclient -y -t -u meeting -w "$AGENTIC_MEETING_TURN_PASSWORD" -p 3478 turn.example.org
openssl s_client -connect turn.example.org:5349 -servername turn.example.org \
    -CAfile /path/to/trusted-ca.pem -verify_hostname turn.example.org \
    -verify_return_error </dev/null
turnutils_uclient -y -t -S \
    -u meeting -w "$AGENTIC_MEETING_TURN_PASSWORD" -p 5349 turn.example.org
```

`turnutils_uclient` 命令在 TURN 上分配两个中继端点并交换数据；测试时对端 ACL 要允许其中继地址。
应看到认证分配成功、收到数据且无异常丢包，错误密码必须失败。在 [coturn 4.7.0 测试客户端](https://github.com/coturn/coturn/blob/4.7.0/src/apps/uclient/mainuclient.c)中，
`turnutils_uclient -E` 单独使用**不会**启用证书校验。用独立
[OpenSSL 步骤](https://docs.openssl.org/3.6/man1/openssl-s_client/)检查 CA 链和域名，
正确 CA/域名必须成功，不信任的 CA 或错误域名必须失败。加密中继成功不能证明服务器身份。
再从目标网络打开真实网页，在浏览器 WebRTC 诊断中检查选中的候选对，验证音频、
字幕及屏幕共享。TURN 工具成功不代表浏览器跨 NAT 已验证。

## 6. 对外开放前

- 启用访问口令，确认未登录的 `GET /api/ice` 和会议 API 返回 401。
- 每台设备使用可信 HTTPS，HTTP 重定向到 HTTPS，登录 Cookie 带 `Secure`。
- 后端 HTTP、推理和管理端口不对外开放，只开放选定的 HTTP 与媒体路径。
- `.env`、`data/`（含 `auth_secret`、录音、截图、任务产物）、TURN 数据库、私钥及备份仅服务账号可读。
  `data/` 不要放进代理静态文档根目录；应用登录不能保护另一个服务暴露的文件。
- 检查 DNS、证书续期、磁盘空间、TURN 可达性与凭据轮换。所有用户共享记录访问权，不应向无权
  查看会议的人开放此实例。
- 从实际客户端网络测试登录、新会议、继续会议、长导出和任务产物下载；避免代理缓存或日志泄露
  凭据及会议内容。

HTTP 可达性可用 `curl --fail https://meeting.example.org/api/auth` 验证，应返回已启用口令的状态。
这只检测网页接口，不代表推理已就绪；配置的推理服务用 `uv run agentic-meeting services status`
检查。

## 7. 故障排查

| 现象                               | 检查                                                                                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 没有麦克风提示或采集 API 不可用    | 使用域名匹配且可信的 HTTPS 证书；检查 `window.isSecureContext`、浏览器站点权限及系统麦克风权限，允许后重试。屏幕采集还需用户操作及浏览器支持。                               |
| 网页打开但 ICE 失败或没有音频      | 查浏览器 WebRTC 诊断及 TURN 日志；确认登录后 `/api/ice` 成功、凭据一致、两端 DNS/监听端口可用，中继端口、对端 ACL、NAT 映射可通。`/api/offer` 成功只代表信令成功。           |
| Mixed content 错误                 | 网页和 API 全部走同一 HTTPS 源，移除 `http://` API/资源地址并使用整站代理。代理到 loopback 的私有 HTTP 跳转不属于浏览器混合内容。                                            |
| 证书警告或 `turns:` 失败           | 分别查网页和 TURN 的域名、证书链、有效期及客户端信任；需要时在每台设备安装组织 CA，不要禁用 TLS 校验。                                                                       |
| 反复登录或 Cookie 没有 `Secure`    | 确认代理发送 `X-Forwarded-Proto: https`，uvicorn 仅信任实际代理地址，浏览器使用同一域名；修改口令环境后重启。                                                                |
| API 返回 401 或 403                | 401 表示缺少或过期登录；重新登录。POST/PATCH/DELETE 的 403 可能是 `X-CSRF-Token` 缺失或过期；网页会发送，自定义客户端须用 `/api/auth` 返回的令牌。代理应保留 Cookie 和该头。 |
| 多人同时收到登录 429               | 等待五分钟窗口结束；核实可信转发客户端地址能区分用户，而非全部显示代理地址。不要信任任意输入头。                                                                             |
| offer 或下载返回 502/504、下载截断 | 检查 loopback 后端是否可达、代理/应用日志、响应头和空闲期限及缓冲，再测慢导出。一小时会议无需让 offer HTTP 响应持续一小时。                                                  |

采集电平与推理故障见[故障排查](troubleshooting.md)。

## 8. 验证范围

使用 Caddy **2.11.7**、nginx **1.28.0**、coturn **4.7.0** 实测，只监听本机，采用临时可信 CA
和生成的凭据；替换测试地址、端口与证书/状态路径，并添加测试专用进程设置。Caddy 使用固定证书
并关闭自动 HTTPS，未测试其自动申请证书/重定向路径。只有隔离的 TURN 测试允许 loopback 对端；
部署示例须继续阻止它们。

- 两种 HTTP 代理均通过真实 TLS、应用登录及 `Secure` Cookie、未登录与缺失 CSRF 拒绝、
  `POST`/`PATCH /api/offer` 转发、`/api/ice` 和完整 5 MiB 分块下载检查；nginx 还转发了
  2 MiB 表单文件上传。应用使用替代媒体 handler，避免启动推理。
- coturn 在客户端 UDP、TCP、TLS 三种传输下通过认证分配与双向中继数据检查；每种传输均拒绝
  错误凭据。独立 OpenSSL 检查接受正确 CA/域名，拒绝不信任的 CA 或错误域名。锁定的服务端 ICE gatherer 对逐个测试的传输均产生中继候选；
  此检查仅把主机地址发现限制为 loopback。

这些属于协议检查。真实浏览器麦克风/屏幕采集、公网 NAT 穿越、生产 DNS、证书申请/续期、
并发容量及生产防火墙/对端 ACL 仍须在实际部署网络上验证；测试没有使用模型或 GPU。
