import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const releaseDir = path.join(root, "release");
const appName = "RegistrationDesk";
const stageDir = path.join(releaseDir, `${appName}-win-x64`);
const appDir = path.join(stageDir, "resources", "app");
const zipPath = path.join(releaseDir, `${appName}-win-x64.zip`);
const electronDist = path.join(root, "node_modules", "electron", "dist");
const exePath = path.join(stageDir, "electron.exe");
const brandedExePath = path.join(stageDir, "Registration Desk.exe");

function run(command, args, cwd) {
  console.log(`> ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { cwd, shell: process.platform === "win32", stdio: "inherit" });
  if (result.error) {
    console.error(result.error);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

if (!existsSync(path.join(electronDist, "electron.exe"))) {
  throw new Error(`Electron runtime not found at ${electronDist}`);
}

rmSync(stageDir, { force: true, recursive: true });
rmSync(zipPath, { force: true });
mkdirSync(releaseDir, { recursive: true });

console.log(`Copying Electron runtime from ${electronDist}`);
cpSync(electronDist, stageDir, { recursive: true });
renameSync(exePath, brandedExePath);

mkdirSync(appDir, { recursive: true });
console.log("Copying compiled app");
cpSync(path.join(root, "dist"), path.join(appDir, "dist"), { recursive: true });

const packageJson = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
const runtimePackage = {
  name: packageJson.name,
  version: packageJson.version,
  private: true,
  type: packageJson.type,
  main: packageJson.main,
  dependencies: packageJson.dependencies
};

writeFileSync(path.join(appDir, "package.json"), `${JSON.stringify(runtimePackage, null, 2)}\n`, "utf8");

run("npm.cmd", ["install", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], appDir);

run("powershell.exe", [
  "-NoProfile",
  "-Command",
  `Compress-Archive -Path '${stageDir}\\*' -DestinationPath '${zipPath}' -Force`
], root);

console.log(`Packaged ${zipPath}`);
