import { readdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { extname, join } from 'node:path';
import { constants, brotliCompressSync, gzipSync } from 'node:zlib';

const DIST_DIR = fileURLToPath(new URL('../dist/', import.meta.url));
const MIN_SIZE = 1024;
const COMPRESSIBLE_EXTENSIONS = new Set([
  '.css',
  '.html',
  '.js',
  '.json',
  '.mjs',
  '.svg',
  '.wasm',
]);

async function collectFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const entryPath = join(directory, entry.name);
    return entry.isDirectory() ? collectFiles(entryPath) : [entryPath];
  }));
  return nested.flat();
}

const files = await collectFiles(DIST_DIR);
await Promise.all(files.map(async (filePath) => {
  if (!COMPRESSIBLE_EXTENSIONS.has(extname(filePath).toLowerCase())) {
    return;
  }
  const content = await readFile(filePath);
  if (content.length < MIN_SIZE) {
    return;
  }
  const gzipContent = gzipSync(content, { level: 9 });
  const brotliContent = brotliCompressSync(content, {
    params: {
      [constants.BROTLI_PARAM_QUALITY]: 11,
    },
  });
  await Promise.all([
    writeFile(`${filePath}.gz`, gzipContent),
    writeFile(`${filePath}.br`, brotliContent),
  ]);
}));
