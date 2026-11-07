# python-so-rebuild

sing-box / cloudflared / nezha-agent 的 c-shared `.so` 加载版 Python 启动器。

`.so` 通过 ctypes 加载（`StartBot`/`StopBot`、`StartTools`/`StopTools`、`StartStatus`/`StopStatus`），
进程内起服务，无需子进程。要求 Linux glibc >= 2.34。

## 发布物（本仓库 Releases）

| 文件 | 内容 | sing-box 版本 |
|---|---|---|
| `bot_amd64.so` / `bot_arm64.so` | sing-box 入站（vless/vmess ws、reality、hy2、tuic、anytls、socks）+ WARP 出站 | v1.14.4 |
| `tools_amd64.so` / `tools_arm64.so` | cloudflared Argo 隧道 | - |
| `status_amd64.so` / `status_arm64.so` | nezha-agent v1 上报 | - |

## 相对基线的补丁

1. pip 按需安装——`.venv` 已有依赖（重启场景）时跳过，重启不依赖联网
2. `cleanup_files` 保留 `.so` 缓存——重启零下载
3. 不删 `.tmp` 目录——部分平台镜像（如 KataBump）的 entrypoint 在 `.tmp` 下生成
   CA bundle 并设置 `REQUESTS_CA_BUNDLE`，删掉会导致进程内所有 HTTPS 请求直接崩
4. 下载源指向本仓库 Releases

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `UUID` | 节点/nezha UUID | 占位，必填 |
| `ARGO_AUTH` | Cloudflare tunnel token 或 TunnelSecret JSON | 空=临时隧道 |
| `ARGO_DOMAIN` | 固定隧道域名 | 空 |
| `ARGO_PORT` | 隧道端口（sing-box 入站监听） | 8888 |
| `NEZHA_SERVER` | 哪吒面板，v1 格式 `host:8008` | - |
| `NEZHA_KEY` | 哪吒 v1 NZ_CLIENT_SECRET | - |
| `PORT` | 订阅 HTTP 端口 | 3000 |
| `REALITY_PORT` / `HY2_PORT` / `TUIC_PORT` / `ANYTLS_PORT` / `S5_PORT` | 对应入站开关 | 空=关 |
| `SHOW_LOG` | 日志开关 | true |
| `NAME` / `CFIP` / `CFPORT` | 节点名前缀 / 优选 IP / 优选端口 | - |

## 用法

```bash
pip install -r requirements.txt
export UUID=<uuid> ARGO_AUTH=<token> ARGO_DOMAIN=<domain>
python app.py
```

订阅写入 `FILE_PATH`（默认 `.cache`）下 `debug.txt`（base64）/`list.txt`（明文）。
