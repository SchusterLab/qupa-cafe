// Runs one of a woken agent's commands in the sandbox.
//
//     node run.mjs <config.json> <command, base64>
//     node run.mjs <config.json> --check
//
// Anthropic's sandbox-runtime used as a library rather than through its `srt`
// CLI, because the CLI reads its allow list from a file whose allowedDomains
// refuses "*": open internet access needs the ask callback below, which
// approves every host. The runtime's own resolved-address check still refuses
// loopback and link-local addresses, and the private ranges are added to it, so
// "every host" means the internet and not this machine or its network.
//
// The command runs in bash with the workspace's pixi environment activated,
// the workspace as HOME, and the agent's directory (config.agent_dirs) as its
// directory. It writes to those two, except the paths config.protected names,
// which steer later wakes. It reads nothing else but those, the projects the
// researcher lets wakes read, the program directories and the system's own
// (SYSTEM_READ). It runs on Linux and macOS, and in WSL, where Windows hands
// its commands. Variables that look like credentials are not passed in.
//
// qupa-install writes config.json; qupa_cafe.sandbox calls this script.

import { SandboxManager } from '@anthropic-ai/sandbox-runtime';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

// In WSL, which Windows hands its commands to.
const IN_WSL = process.platform === 'linux' && Boolean(process.env.WSL_DISTRO_NAME);

const PRIVATE_RANGES = [
  '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '169.254.0.0/16',
  '127.0.0.0/8', '0.0.0.0/8', 'fc00::/7', 'fe80::/10', '::1/128',
];

// What a command reads besides its own directories: the system's programs,
// libraries and configuration, and none of the places people keep files.
const SYSTEM_READ = {
  linux: ['/usr', '/bin', '/sbin', '/lib', '/lib32', '/lib64', '/libx32', '/etc'],
  darwin: ['/usr', '/bin', '/sbin', '/System', '/Library', '/private/etc', '/private/var/db', '/opt/homebrew'],
};

// Removed from the command's environment by name.
const SECRET = /^(QUPA_|ANTHROPIC_|CLAUDE|OPENAI_|CODEX_|AWS_|AZURE_|GOOGLE_|GCLOUD_|GH_|GITHUB_|HF_|HUGGING|NPM_|PYPI_|TWINE_|SSH_AUTH)|TOKEN|SECRET|PASSW|API_?KEY|CREDENTIAL|_KEY$/i;

// What would make the sandboxed Python something other than the workspace's.
const FOREIGN = /^(PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|PYTHONUSERBASE|VIRTUAL_ENV|CONDA_.*|PIXI_.*|PIP_.*|NODE_OPTIONS|LD_PRELOAD|LD_LIBRARY_PATH|DYLD_.*)$/i;

function fail(message) {
  console.error(`qupa-cafe sandbox: ${message}`);
  process.exit(126);
}

function quote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function sandboxConfig(config, bridge) {
  const network = { allowedDomains: [], deniedDomains: [], deniedResolvedAddresses: PRIVATE_RANGES };
  return {
    network,
    filesystem: {
      denyRead: ['/'],
      // This directory too: the runtime's seccomp helper runs inside. And
      // the directories its programs come from (config.programs), which
      // include pixi's whole install, since a command pixi exposes is a
      // trampoline into an environment beside its bin directory.
      allowRead: [
        ...(SYSTEM_READ[process.platform] ?? []),
        config.workspace,
        path.dirname(config.run),
        path.dirname(config.pixi),
        ...(config.programs ?? []),
        ...(config.agent_dirs ?? []),
        // Projects the researcher let wakes read, and not change.
        ...(config.read_dirs ?? []),
        // The runtime's own temporary directory, which holds the sockets its
        // network bridge mounts inside: under the read deny of '/', the
        // command would find an empty /tmp in its place and no internet.
        bridge,
      ],
      allowWrite: [config.workspace, ...(config.agent_dirs ?? [])],
      denyWrite: config.protected ?? [],
    },
  };
}

function prelude(config) {
  const ws = config.workspace;
  const lines = [
    `export HOME=${quote(ws)}`,
    `export TMPDIR=${quote(path.join(ws, '.tmp'))} TMP=${quote(path.join(ws, '.tmp'))} TEMP=${quote(path.join(ws, '.tmp'))}`,
    `export PIXI_CACHE_DIR=${quote(path.join(ws, '.cache', 'rattler'))}`,
    // No display: a figure is saved, never shown.
    'export MPLBACKEND=Agg',
    `export PATH=${quote(path.dirname(config.pixi))}:"$PATH"`,
    `eval "$(pixi shell-hook --manifest-path ${quote(path.join(ws, 'pixi.toml'))} --shell bash)" || exit 1`,
    `export PATH=${quote(path.join(ws, '.qupa-bin'))}:"$PATH"`,
    `cd ${quote((config.agent_dirs ?? [])[0] ?? ws)} || exit 1`,
  ];
  return lines.join('\n');
}

function cleanEnv() {
  const env = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (!SECRET.test(name) && !FOREIGN.test(name)) env[name] = value;
  }
  if (IN_WSL) {
    // Not Windows' programs, which WSL appends to PATH, nor the socket WSL
    // starts one through, which would run it on Windows as the researcher.
    delete env.WSL_INTEROP;
    env.PATH = (env.PATH ?? '').split(':').filter(dir => !dir.startsWith('/mnt/')).join(':');
  }
  return env;
}

async function run(config, command) {
  // The directories setup found the runtime's own programs in, first.
  process.env.PATH = [...(config.path ?? []), process.env.PATH].join(path.delimiter);
  // The ask callback is the open internet: every host the allow list does not
  // name, which is every host, is approved.
  const bridge = mkdtempSync(path.join(tmpdir(), 'qupa-cafe-'));
  process.env.TMPDIR = bridge;
  await SandboxManager.initialize(sandboxConfig(config, bridge), async () => true);

  const script = `${prelude(config)}\n${command}`;
  const env = cleanEnv();
  const wrapped = await SandboxManager.wrapWithSandbox(script, config.bash);
  const child = spawn(wrapped, { shell: true, stdio: 'inherit', env, cwd: config.workspace });

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
  rmSync(bridge, { recursive: true, force: true });
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
  if (!configPath || !what) fail('usage: node run.mjs <config.json> <command in base64> | --check');

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
