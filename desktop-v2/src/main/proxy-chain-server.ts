import net from "node:net";
import tls from "node:tls";
import http from "node:http";
import type { ProxySettings } from "./proxy.js";

interface ProxyEndpoint {
  protocol: "http:" | "https:" | "socks5:";
  host: string;
  port: number;
  username: string;
  password: string;
}

const PROXY_CONNECT_TIMEOUT_MS = 8_000;
const PROXY_HANDSHAKE_TIMEOUT_MS = 10_000;

export interface BrowserProxyRoute {
  proxy: ProxySettings | undefined;
  label: string;
  close(): Promise<void>;
}

export async function prepareBrowserProxy(localProxy: string, dynamicProxy: string): Promise<BrowserProxyRoute> {
  const values = [localProxy, dynamicProxy].map((value) => value.trim()).filter(Boolean);
  if (!values.length) return { proxy: undefined, label: "直连", close: async () => undefined };
  const endpoints = values.map(parseProxyEndpoint);
  const needsBridge = endpoints.length > 1 || endpoints[0].protocol === "socks5:" && Boolean(endpoints[0].username);
  if (!needsBridge) {
    const endpoint = endpoints[0];
    return {
      proxy: {
        server: `${endpoint.protocol}//${endpoint.host}:${endpoint.port}`,
        username: endpoint.username || undefined,
        password: endpoint.password || undefined,
      },
      label: maskEndpoint(endpoint),
      close: async () => undefined,
    };
  }
  const server = new ConnectChainServer(endpoints);
  const url = await server.start();
  return {
    proxy: { server: url },
    label: endpoints.map(maskEndpoint).join(" -> "),
    close: () => server.close(),
  };
}

export class ConnectChainServer {
  private readonly server = http.createServer((_request, response) => {
    response.writeHead(405, { connection: "close" });
    response.end("CONNECT only");
  });
  private sockets = new Set<net.Socket>();

  constructor(private readonly proxies: ProxyEndpoint[]) {
    if (!proxies.length) throw new Error("代理链不能为空");
    this.server.on("connect", (request, client, head) => void this.handleConnect(request, client as net.Socket, head));
    this.server.on("connection", (socket) => {
      this.sockets.add(socket);
      socket.once("close", () => this.sockets.delete(socket));
    });
  }

  async start(): Promise<string> {
    await new Promise<void>((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(0, "127.0.0.1", () => resolve());
    });
    const address = this.server.address();
    if (!address || typeof address === "string") throw new Error("链式代理监听失败");
    return `http://127.0.0.1:${address.port}`;
  }

  async close(): Promise<void> {
    for (const socket of this.sockets) socket.destroy();
    if (!this.server.listening) return;
    const closed = new Promise<void>((resolve) => this.server.close(() => resolve()));
    this.server.closeAllConnections?.();
    this.server.closeIdleConnections?.();
    await Promise.race([closed, new Promise<void>((resolve) => setTimeout(resolve, 1000))]);
  }

  private async handleConnect(request: http.IncomingMessage, client: net.Socket, head: Buffer): Promise<void> {
    try {
      const target = parseHostPort(request.url || "", 443);
      let upstream = await connectToProxy(this.proxies[0]);
      this.trackSocket(upstream);
      for (let index = 0; index < this.proxies.length; index += 1) {
        const proxy = this.proxies[index];
        const destination = index + 1 < this.proxies.length
          ? { host: this.proxies[index + 1].host, port: this.proxies[index + 1].port }
          : target;
        if (proxy.protocol === "socks5:") await socks5Connect(upstream, proxy, destination.host, destination.port);
        else await httpConnect(upstream, proxy, destination.host, destination.port);
        if (index + 1 < this.proxies.length && this.proxies[index + 1].protocol === "https:") {
          upstream = await wrapTls(upstream, this.proxies[index + 1].host);
          this.trackSocket(upstream);
        }
      }
      client.write("HTTP/1.1 200 Connection Established\r\nProxy-Agent: RegistrationDesk\r\n\r\n");
      if (head.length) upstream.write(head);
      upstream.on("error", () => client.destroy());
      client.on("error", () => upstream.destroy());
      upstream.pipe(client);
      client.pipe(upstream);
    } catch (error) {
      if (!client.destroyed) {
        client.end(`HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\n${(error as Error).message}`);
      }
    }
  }

  private trackSocket(socket: net.Socket): void {
    this.sockets.add(socket);
    socket.once("close", () => this.sockets.delete(socket));
  }
}

export function parseProxyEndpoint(value: string): ProxyEndpoint {
  let text = value.trim();
  if (!text.includes("://")) {
    if (text.includes("@")) text = `http://${text}`;
    else {
      const parts = text.split(":");
      if (parts.length === 4) text = `http://${encodeURIComponent(parts[2])}:${encodeURIComponent(parts[3])}@${parts[0]}:${parts[1]}`;
      else text = `http://${text}`;
    }
  }
  const url = new URL(text);
  if (!["http:", "https:", "socks5:"].includes(url.protocol)) throw new Error(`不支持的代理协议: ${url.protocol}`);
  const port = Number(url.port || (url.protocol === "https:" ? 443 : url.protocol === "socks5:" ? 1080 : 80));
  if (!url.hostname || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`代理地址无效: ${value}`);
  return {
    protocol: url.protocol as ProxyEndpoint["protocol"],
    host: url.hostname,
    port,
    username: decodeURIComponent(url.username),
    password: decodeURIComponent(url.password),
  };
}

function connectToProxy(proxy: ProxyEndpoint): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const raw = net.connect(proxy.port, proxy.host);
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      raw.removeListener("error", onError);
      raw.removeListener("timeout", onTimeout);
      callback();
    };
    const onError = (error: Error) => finish(() => reject(error));
    const onTimeout = () => finish(() => {
      raw.destroy();
      reject(new Error(`proxy TCP connect timeout: ${proxy.host}:${proxy.port}`));
    });
    raw.setTimeout(PROXY_CONNECT_TIMEOUT_MS);
    raw.once("error", onError);
    raw.once("timeout", onTimeout);
    raw.once("connect", () => {
      finish(() => {
        raw.setTimeout(0);
        if (proxy.protocol !== "https:") { resolve(raw); return; }
        wrapTls(raw, proxy.host).then(resolve, reject);
      });
    });
  });
}

function wrapTls(socket: net.Socket, servername: string): Promise<tls.TLSSocket> {
  return new Promise((resolve, reject) => {
    const secure = tls.connect({ socket, servername });
    secure.once("secureConnect", () => resolve(secure));
    secure.once("error", reject);
  });
}

async function httpConnect(socket: net.Socket, proxy: ProxyEndpoint, host: string, port: number): Promise<void> {
  const authority = formatAuthority(host, port);
  const auth = proxy.username ? `Proxy-Authorization: Basic ${Buffer.from(`${proxy.username}:${proxy.password}`).toString("base64")}\r\n` : "";
  socket.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\n${auth}Proxy-Connection: Keep-Alive\r\n\r\n`);
  const response = await readUntil(socket, Buffer.from("\r\n\r\n"), 64 * 1024);
  const header = response.data.subarray(0, response.index + 4).toString("latin1");
  const match = header.match(/^HTTP\/\d(?:\.\d)?\s+(\d+)/i);
  if (!match || Number(match[1]) !== 200) throw new Error(`代理 CONNECT 失败: ${header.split("\r\n")[0] || "无响应"}`);
  const remainder = response.data.subarray(response.index + 4);
  if (remainder.length) socket.unshift(remainder);
}

async function socks5Connect(socket: net.Socket, proxy: ProxyEndpoint, host: string, port: number): Promise<void> {
  const methods = proxy.username ? [0x00, 0x02] : [0x00];
  socket.write(Buffer.from([0x05, methods.length, ...methods]));
  const greeting = await readExact(socket, 2);
  if (greeting[0] !== 0x05 || greeting[1] === 0xff) throw new Error("SOCKS5 代理拒绝认证方式");
  if (greeting[1] === 0x02) {
    const username = Buffer.from(proxy.username);
    const password = Buffer.from(proxy.password);
    if (username.length > 255 || password.length > 255) throw new Error("SOCKS5 用户名或密码过长");
    socket.write(Buffer.concat([Buffer.from([0x01, username.length]), username, Buffer.from([password.length]), password]));
    const auth = await readExact(socket, 2);
    if (auth[1] !== 0x00) throw new Error("SOCKS5 用户名或密码错误");
  }
  const hostBuffer = Buffer.from(host);
  if (hostBuffer.length > 255) throw new Error("SOCKS5 目标主机名过长");
  socket.write(Buffer.concat([
    Buffer.from([0x05, 0x01, 0x00, 0x03, hostBuffer.length]),
    hostBuffer,
    Buffer.from([(port >> 8) & 0xff, port & 0xff]),
  ]));
  const header = await readExact(socket, 4);
  if (header[0] !== 0x05 || header[1] !== 0x00) throw new Error(`SOCKS5 CONNECT 失败: ${header[1]}`);
  const addressLength = header[3] === 0x01 ? 4 : header[3] === 0x04 ? 16 : (await readExact(socket, 1))[0];
  await readExact(socket, addressLength + 2);
}

function readUntil(socket: net.Socket, marker: Buffer, maximum: number): Promise<{ data: Buffer; index: number }> {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("proxy handshake timeout"));
    }, PROXY_HANDSHAKE_TIMEOUT_MS);
    const cleanup = () => { clearTimeout(timer); socket.off("data", onData); socket.off("error", onError); socket.off("close", onClose); };
    const onError = (error: Error) => { cleanup(); reject(error); };
    const onClose = () => { cleanup(); reject(new Error("代理连接提前关闭")); };
    const onData = (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      const index = buffer.indexOf(marker);
      if (index >= 0) { cleanup(); resolve({ data: buffer, index }); }
      else if (buffer.length > maximum) { cleanup(); reject(new Error("代理响应头过大")); }
    };
    socket.on("data", onData); socket.once("error", onError); socket.once("close", onClose);
  });
}

function readExact(socket: net.Socket, length: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("proxy handshake timeout"));
    }, PROXY_HANDSHAKE_TIMEOUT_MS);
    const cleanup = () => { clearTimeout(timer); socket.off("data", onData); socket.off("error", onError); socket.off("close", onClose); };
    const onError = (error: Error) => { cleanup(); reject(error); };
    const onClose = () => { cleanup(); reject(new Error("代理连接提前关闭")); };
    const onData = (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length < length) return;
      cleanup();
      const remainder = buffer.subarray(length);
      if (remainder.length) socket.unshift(remainder);
      resolve(buffer.subarray(0, length));
    };
    socket.on("data", onData); socket.once("error", onError); socket.once("close", onClose);
  });
}

function parseHostPort(value: string, fallbackPort: number): { host: string; port: number } {
  const url = new URL(`http://${value}`);
  return { host: url.hostname, port: Number(url.port || fallbackPort) };
}

function formatAuthority(host: string, port: number): string { return `${host.includes(":") ? `[${host}]` : host}:${port}`; }
function maskEndpoint(endpoint: ProxyEndpoint): string { return `${endpoint.protocol}//${endpoint.username ? `${endpoint.username}:***@` : ""}${endpoint.host}:${endpoint.port}`; }
