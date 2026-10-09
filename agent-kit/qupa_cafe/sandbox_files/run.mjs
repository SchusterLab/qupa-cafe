// Runs one of a woken agent's commands in the sandbox.
//
//     node run.mjs <config.json> <command, base64>
//     node run.mjs <config.json> --check
//     node run.mjs <config.json> --windows-status
//
// Anthropic's sandbox-runtime used as a library rather than through its `srt`
// CLI, because the CLI reads its allow list from a file whose allowedDomains
// refuses "*": open internet access needs the ask callback below, which
// approves every host. The runtime's own resolved-address check still refuses
// loopback and link-local addresses, and the private ranges are added to it, so
// "every host" means the internet and not this machine or its network.
//
// The command runs in bash with the workspace's pixi environment activated and
// the workspace as its directory and HOME. Writes go to the workspace only.
// Reads: on Linux and macOS the home directory is denied apart from the
// workspace and the directories config.readable lists; on Windows the command
// runs as the runtime's `srt-sandbox` user, which reads only what qupa-install
// granted it. Variables that look like credentials are not passed in.
//
// qupa-install writes config.json; qupa_cafe.sandbox calls this script.

import { SandboxManager, VENDORED_SRT_WIN_EXE, checkWindowsSandboxStatus, resolveSrtWin } from '@anthropic-ai/sandbox-runtime';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const WINDOWS = process.platform === 'win32';

const PRIVATE_RANGES = [
  '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '169.254.0.0/16',
  '127.0.0.0/8', '0.0.0.0/8', 'fc00::/7', 'fe80::/10', '::1/128',
];

// Removed from the command's environment by name.
const SECRET = /^(QUPA_|ANTHROPIC_|CLAUDE|OPENAI_|CODEX_|AWS_|AZURE_|GOOGLE_|GCLOUD_|GH_|GITHUB_|HF_|HUGGING|NPM_|PYPI_|TWINE_|SSH_AUTH)|TOKEN|SECRET|PASSW|API_?KEY|CREDENTIAL|_KEY$/i;

// What would make the sandboxed Python something other than the workspace's.
const FOREIGN = /^(PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|PYTHONUSERBASE|VIRTUAL_ENV|CONDA_.*|PIXI_.*|PIP_.*|NODE_OPTIONS|LD_PRELOAD|LD_LIBRARY_PATH|DYLD_.*)$/i;

function fail(message) {
  console.error(`qupa-cafe sandbox: ${message}`);
  process.exit(126);
}

// A path as Git Bash on Windows writes one, for PATH: C:\a\b is /c/a/b.
function bashPath(p) {
  if (!WINDOWS) return p;
  const m = /^([A-Za-z]):[\\/](.*)$/.exec(p);
  return m ? `/${m[1].toLowerCase()}/${m[2].replace(/\\/g, '/')}` : p.replace(/\\/g, '/');
}

function quote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function sandboxConfig(config) {
  const network = { allowedDomains: [], deniedDomains: [], deniedResolvedAddresses: PRIVATE_RANGES };
  if (WINDOWS) {
    // The srt-sandbox user's reads and writes are the ACEs qupa-install
    // granted once. Granting them here would re-stamp the whole environment
    // on every command, which takes a minute.
    return {
      network,
      filesystem: { denyRead: [], allowRead: [], allowWrite: [], denyWrite: [] },
      windows: { srtWin: { path: VENDORED_SRT_WIN_EXE } },
    };
  }
  return {
    network,
    filesystem: {
      denyRead: [homedir()],
      // This directory too: the runtime's seccomp helper runs inside.
      allowRead: [config.workspace, path.dirname(config.run), path.dirname(config.pixi), ...(config.readable ?? [])],
      allowWrite: [config.workspace],
      denyWrite: [],
    },
  };
}

function prelude(config) {
  const ws = config.workspace;
  const lines = [
    `export HOME=${quote(bashPath(ws))}`,
    `export TMPDIR=${quote(bashPath(path.join(ws, '.tmp')))} TMP=${quote(path.join(ws, '.tmp'))} TEMP=${quote(path.join(ws, '.tmp'))}`,
    `export PIXI_CACHE_DIR=${quote(path.join(ws, '.cache', 'rattler'))}`,
    `export PATH=${quote(bashPath(path.dirname(config.pixi)))}:"$PATH"`,
    `eval "$(pixi shell-hook --manifest-path ${quote(path.join(ws, 'pixi.toml'))} --shell bash)" || exit 1`,
    `export PATH=${quote(bashPath(path.join(ws, '.qupa-bin')))}:"$PATH"`,
    `cd ${quote(bashPath(ws))} || exit 1`,
  ];
  return lines.join('\n');
}

function cleanEnv() {
  const env = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (!SECRET.test(name) && !FOREIGN.test(name)) env[name] = value;
  }
  return env;
}

async function run(config, command) {
  // The ask callback is the open internet: every host the allow list does not
  // name, which is every host, is approved.
  await SandboxManager.initialize(sandboxConfig(config), async () => true);

  const script = `${prelude(config)}\n${command}`;
  const env = cleanEnv();
  let child;
  if (WINDOWS) {
    const before = process.env;
    process.env = env;
    const wrapped = await SandboxManager.wrapWithSandboxArgv(script, config.bash, undefined, undefined, config.workspace);
    process.env = before;
    child = spawn(wrapped.argv[0], wrapped.argv.slice(1), { shell: false, stdio: 'inherit', env: wrapped.env, cwd: config.workspace });
  } else {
    const wrapped = await SandboxManager.wrapWithSandbox(script, config.bash);
    child = spawn(wrapped, { shell: true, stdio: 'inherit', env, cwd: config.workspace });
  }

  const forward = signal => child.kill(signal);
  process.on('SIGINT', forward);
  process.on('SIGTERM', forward);
  const code = await new Promise(resolve => {
    child.on('exit', (status, signal) => resolve(status ?? (signal ? 128 : 1)));
    child.on('error', error => {
      console.error(`qupa-cafe sandbox: ${error.message}`);
      resolve(126);
    });
  });
  SandboxManager.cleanupAfterCommand?.();
  await SandboxManager.reset();
  return code;
}

const CHECK = [
  'python - <<"EOF"',
  'import os, scipy, urllib.request',
  'urllib.request.urlopen("https://pypi.org/simple/", timeout=30).read(1)',
  'for name in ("QUPA_API_KEY", "ANTHROPIC_API_KEY"):',
  '    assert name not in os.environ, name',
  'print(f"scipy {scipy.__version__}, internet reachable")',
  'EOF',
].join('\n');

async function main() {
  const [configPath, what] = process.argv.slice(2);
  if (!configPath || !what) fail('usage: node run.mjs <config.json> <command in base64> | --check | --windows-status');

  if (what === '--windows-status') {
    console.log(JSON.stringify(checkWindowsSandboxStatus({ srtWin: resolveSrtWin({ path: VENDORED_SRT_WIN_EXE }) })));
    return 0;
  }

  let config;
  try {
    config = JSON.parse(readFileSync(configPath, 'utf8'));
  } catch (error) {
    fail(`cannot read ${configPath}: ${error.message}. Run qupa-install.`);
  }
  const command = what === '--check' ? CHECK : Buffer.from(what, 'base64').toString('utf8');
  try {
    return await run(config, command);
  } catch (error) {
    fail(`${error.message ?? error}`);
  }
}

process.exit(await main());
