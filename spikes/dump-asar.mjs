#!/usr/bin/env node
/**
 * dump-asar.mjs — copy selected @deepseek-ai packages out of the installed
 * DeepSeek Harness app.asar so they can be grepped/read with ordinary tools.
 *
 * Run it with the bundled Electron binary acting as Node (that is exactly how
 * the shipped `dsh` launcher runs, and only that runtime patches fs for asar):
 *
 *   ELECTRON_RUN_AS_NODE=1 "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness" \
 *     spikes/dump-asar.mjs [outDir] [pkg ...]
 *
 * Evidence use: T1 read the ACP bridge (`dsh-acp`), session format
 * (`dsh-session-format*`) and JSONL persistence (`dsh-session-persistence-jsonl`)
 * sources this way. Outputs are read-only copies; never edit them.
 */
import fs from 'node:fs';
import path from 'node:path';

const ASAR_DROP = '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/';
const PKG_ROOT = ASAR_DROP + 'dsh/node_modules/@deepseek-ai/';

const outDir = process.argv[2] ?? new URL('./_asar_src/', import.meta.url).pathname;
const pkgs = process.argv.slice(3).length
  ? process.argv.slice(3)
  : [
      'dsh-acp',
      'dsh-session-format',
      'dsh-session-format-v3-to-v4',
      'dsh-session-persistence-jsonl',
      'dsh-session-persistence',
      'dsh-session',
      'dsh-session-projection',
      'dsh-web',
      'dsh-host-webserver',
      'dsh-api-remote-access',
    ];

function walk(src, rel, acc) {
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const relPath = rel ? rel + '/' + entry.name : entry.name;
    if (entry.isDirectory()) walk(path.join(src, entry.name), relPath, acc);
    else acc.push(relPath);
  }
  return acc;
}

fs.mkdirSync(outDir, { recursive: true });
for (const pkg of pkgs) {
  const src = PKG_ROOT + pkg + '/';
  if (!fs.existsSync(src)) {
    console.error(`[skip] ${pkg}: not present in asar`);
    continue;
  }
  const files = walk(src, '', []);
  for (const rel of files) {
    const dst = path.join(outDir, pkg, rel);
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(src + rel, dst);
  }
  console.log(`[ok] ${pkg}: ${files.length} files`);
}
console.log(`done -> ${outDir}`);
