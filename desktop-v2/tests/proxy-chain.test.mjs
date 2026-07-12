import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import { ConnectChainServer, parseProxyEndpoint } from "../dist/main/proxy-chain-server.js";

test("chains local and dynamic HTTP CONNECT proxies", { timeout: 10_000 }, async () => {
  const target = net.createServer((socket) => socket.pipe(socket));
  const first = makeConnectProxy();
  const second = makeConnectProxy();
  await Promise.all([listen(target), listen(first), listen(second)]);
  const targetPort = portOf(target);
  const firstPort = portOf(first);
  const secondPort = portOf(second);
  const chain = new ConnectChainServer([
    parseProxyEndpoint(`http://127.0.0.1:${firstPort}`),
    parseProxyEndpoint(`http://127.0.0.1:${secondPort}`),
  ]);
  try {
    const chainUrl = new URL(await chain.start());
    const echoed = await tunnelAndEcho(Number(chainUrl.port), `127.0.0.1:${targetPort}`, "chain-ok");
    assert.equal(echoed, "chain-ok");
  } finally {
    await chain.close();
    await Promise.all([closeServer(first), closeServer(second), closeServer(target)]);
  }
});

test("parses authenticated SOCKS5 endpoints", () => {
  assert.deepEqual(parseProxyEndpoint("socks5://user:pass@proxy.test:1080"), {
    protocol: "socks5:", host: "proxy.test", port: 1080, username: "user", password: "pass",
  });
});

function makeConnectProxy() {
  const server = http.createServer((_request, response) => { response.writeHead(405); response.end(); });
  server.on("connect", (request, client, head) => {
    const target = new URL(`http://${request.url}`);
    const upstream = net.connect(Number(target.port), target.hostname, () => {
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) upstream.write(head);
      upstream.pipe(client);
      client.pipe(upstream);
    });
    upstream.on("error", () => client.destroy());
  });
  return server;
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
}

function portOf(server) { return server.address().port; }

function closeServer(server) {
  if (!server.listening) return Promise.resolve();
  server.closeAllConnections?.();
  server.closeIdleConnections?.();
  return new Promise((resolve) => server.close(resolve));
}

function tunnelAndEcho(proxyPort, authority, payload) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(proxyPort, "127.0.0.1", () => socket.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\n\r\n`));
    socket.setTimeout(5000, () => { reject(new Error("tunnel echo timeout")); socket.destroy(); });
    let buffer = Buffer.alloc(0);
    let connected = false;
    socket.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (!connected) {
        const index = buffer.indexOf("\r\n\r\n");
        if (index < 0) return;
        const header = buffer.subarray(0, index).toString("latin1");
        if (!/^HTTP\/1\.1 200/.test(header)) { reject(new Error(header)); socket.destroy(); return; }
        connected = true;
        buffer = buffer.subarray(index + 4);
        socket.write(payload);
      }
      if (connected && buffer.toString().includes(payload)) {
        resolve(payload);
        socket.destroy();
      }
    });
    socket.once("error", reject);
  });
}
