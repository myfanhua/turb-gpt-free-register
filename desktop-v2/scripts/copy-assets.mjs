import fs from "node:fs/promises";
import path from "node:path";

const source = path.resolve("src/renderer");
const target = path.resolve("dist/renderer");
await fs.mkdir(target, { recursive: true });
for (const name of ["index.html", "styles.css"]) {
  await fs.copyFile(path.join(source, name), path.join(target, name));
}
await fs.mkdir(path.resolve("dist/preload"), { recursive: true });
await fs.copyFile(path.resolve("src/preload/preload.cjs"), path.resolve("dist/preload/preload.cjs"));
