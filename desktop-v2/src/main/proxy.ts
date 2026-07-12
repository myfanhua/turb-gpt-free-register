export interface ProxySettings { server: string; username?: string; password?: string }

export function parseProxy(value: string): ProxySettings | undefined {
  const text = value.trim();
  if (!text) return undefined;
  if (text.includes("://")) {
    const url = new URL(text);
    return {
      server: `${url.protocol}//${url.hostname}:${url.port}`,
      username: decodeURIComponent(url.username),
      password: decodeURIComponent(url.password),
    };
  }
  if (text.includes("@")) return parseProxy(`http://${text}`);
  const parts = text.split(":");
  if (parts.length === 4) {
    const [host, port, username, password] = parts;
    return { server: `http://${host}:${port}`, username, password };
  }
  if (parts.length === 2) return { server: `http://${text}` };
  throw new Error(`代理格式无法识别: ${text}`);
}

export function proxyLines(localProxy: string, pool: string): string[] {
  const dynamic = splitProxyPool(pool);
  return dynamic.length ? dynamic : localProxy.trim() ? [localProxy.trim()] : [];
}

export function splitProxyPool(pool: string): string[] {
  return pool.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

export function takeDynamicProxies(pool: string, accountCount: number): { taken: string[]; remaining: string[] } {
  const lines = splitProxyPool(pool);
  if (lines.length > 0 && lines.length < accountCount) {
    throw new Error(`本次选择 ${accountCount} 个账号，但动态代理只有 ${lines.length} 个；请补足代理或清空代理池后明确使用本地代理/直连`);
  }
  return { taken: lines.slice(0, accountCount), remaining: lines.slice(accountCount) };
}

export function resolveTaskConcurrency(localProxy: string, dynamicProxies: string[], requested: number): number {
  const value = Math.min(20, Math.max(1, Math.trunc(requested || 1)));
  if (dynamicProxies.length) return Math.min(value, dynamicProxies.length);
  if (localProxy.trim()) return value;
  return 1;
}
