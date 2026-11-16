import { cp, copyFile, mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const packageRoot = dirname(require.resolve("pdfjs-dist/package.json"));
const { version } = JSON.parse(await readFile(join(packageRoot, "package.json"), "utf8"));
const destination = fileURLToPath(new URL(`../public/pdfjs/${version}/`, import.meta.url));
await mkdir(destination, { recursive: true });
await copyFile(join(packageRoot, "build/pdf.worker.min.mjs"), join(destination, "pdf.worker.min.mjs"));
await copyFile(join(packageRoot, "LICENSE"), join(destination, "LICENSE"));
for (const directory of ["cmaps", "standard_fonts", "wasm"]) {
  await cp(join(packageRoot, directory), join(destination, directory), { recursive: true });
}
process.stdout.write(`Prepared local PDF.js ${version} assets.\n`);
